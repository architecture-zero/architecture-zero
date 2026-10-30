"""A turn sent to a bounded local window fits it: the oldest history goes
first, the system prompt and the current message never (2026-10-29).

The chat's history strategy defaults to "warn", which trims nothing, and the
input bound admits about 50000 tokens of prompt and history
(CHAT_MAX_INPUT_CHARS, 200000 characters, at four a token). On the local
Ollama lane the window is num_ctx (providers.OLLAMA_NUM_CTX, 32768 tokens by
default), so a long conversation overflowed it, and an over-long prompt is
cut from the FRONT, where the system prompt and its safety rules are.

What these pin, on that lane: what is sent, with the reply's room and - when
tools ride - the turn's tool budget, fits the window; the system message is
the one a short conversation gets, first and whole; the current message is
last and whole; what goes is the oldest history, and no more than has to;
one log line says so, with counts and no text. With tools on, the whole turn
- history, five rounds of full reads, the reply - still fits. CONTROL: a
short conversation is sent exactly as before; a cloud lane gets the whole
history as before.
"""
import json
from unittest.mock import patch

from app import agent
from app.providers import OLLAMA_NUM_CTX
from tests.test_agent_tool_results_budget import _big_files, _tokens, _ws

REPLY_TOKENS = 4096   # a signed-in turn's max_tokens; it shares the window
PROMPT = "What did we decide about the release?"
LOCAL = "test-model"   # no vendor prefix and no namespace: the Ollama lane


def _history(n, size):
    """n alternating messages of `size` characters, numbered."""
    out = []
    for i in range(n):
        head = f"[message {i:03d}] "
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": head + "x" * (size - len(head))})
    return out


def _send(client, headers, history, session, model=LOCAL, rounds=0):
    """One chat turn through the real route; the provider stream is stubbed.
    `rounds` tool rounds each read big<n>.log before the final answer."""
    sink: dict = {"events": []}
    calls = {"n": 0}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        calls["n"] += 1
        if calls["n"] == 1:
            sink["first"] = [dict(m) for m in messages]
            sink["tools"] = tools
        if calls["n"] <= rounds:
            yield {"type": "tool_call", "id": f"t{calls['n']}", "name": "read_file",
                   "args": {"path": f"big{calls['n'] - 1}.log"}}
            return
        sink["last"] = [dict(m) for m in messages]
        yield {"type": "text", "text": "done"}

    from app.routers import chat as chat_mod
    real_log = chat_mod.log

    def _log(event, **fields):
        sink["events"].append((event, fields))
        return real_log(event, **fields)

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.log", side_effect=_log):
        r = client.post("/api/chat", headers=headers, json={
            "prompt": PROMPT, "model": model, "use_rag": False,
            "session_id": session, "history": history})
    assert r.status_code == 200, r.text
    assert "done" in r.text
    return sink


def _trims(sink):
    return [f for e, f in sink["events"] if e == "chat_history_trimmed"]


# -- FIX ----------------------------------------------------------------------

def test_a_long_conversation_on_the_local_lane_fits_the_window(client, admin_headers):
    history = _history(40, 4000)          # about 40000 tokens: past the window alone
    assert _tokens(history) > OLLAMA_NUM_CTX
    sink = _send(client, admin_headers, history, "s-window-fix")
    assert not sink["tools"]
    sent = sink["first"]
    system, kept, last = sent[0], sent[1:-1], sent[-1]
    # The current message last and whole; the oldest history gone, the
    # newest kept, in order.
    assert last == {"role": "user", "content": PROMPT}
    assert 0 < len(kept) < len(history), len(kept)
    assert kept == history[-len(kept):]
    # What is sent, and the reply's room, fit the window ...
    assert _tokens(sent) + REPLY_TOKENS <= OLLAMA_NUM_CTX, _tokens(sent)
    # ... and no more went than had to: one more message would not fit.
    one_more = [system, history[-len(kept) - 1], *kept, last]
    assert _tokens(one_more) + REPLY_TOKENS > OLLAMA_NUM_CTX
    # The system prompt is the one a short conversation gets, first and whole.
    short = _send(client, admin_headers, history[-2:], "s-window-fix-short")
    assert system == short["first"][0]
    assert system["role"] == "system" and "message 0" not in system["content"]
    # One log line, counts only.
    trims = _trims(sink)
    assert len(trims) == 1, trims
    assert trims[0]["dropped"] == len(history) - len(kept)
    assert trims[0]["kept"] == len(kept)
    said = json.dumps(trims[0])
    assert "xxxx" not in said and PROMPT not in said and "[message" not in said


def test_with_tools_the_whole_turn_fits(client, admin_headers, monkeypatch, tmp_path):
    """Five rounds of full reads on top of a long conversation: the history
    leaves room for the turn's tool budget, so the last round still fits."""
    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 5)
    history = _history(40, 4000)
    sink = _send(client, admin_headers, history, "s-window-tools", rounds=5)
    assert sink["tools"]
    sent = sink["last"]
    assert sent[0] == sink["first"][0] and sent[0]["role"] == "system"
    kept = [m for m in sent[1:] if m["role"] in ("user", "assistant")
            and m.get("content", "").startswith("[message")]
    assert 0 < len(kept) < len(history)
    assert kept == history[-len(kept):]
    tool_msgs = [m for m in sent if m.get("role") == "tool"]
    assert len(tool_msgs) == 5
    definitions = len(json.dumps(sink["tools"])) // 4
    assert _tokens(sent) + definitions + REPLY_TOKENS <= OLLAMA_NUM_CTX, _tokens(sent)
    assert len(_trims(sink)) == 1


# -- CONTROL ------------------------------------------------------------------

def test_a_short_conversation_is_sent_exactly_as_before(client, admin_headers):
    history = _history(4, 300)
    sink = _send(client, admin_headers, history, "s-window-short")
    sent = sink["first"]
    assert sent[1:] == [*history, {"role": "user", "content": PROMPT}]
    assert sent[0]["role"] == "system"
    assert _trims(sink) == []


def test_a_cloud_lane_gets_the_whole_history_as_before(client, admin_headers):
    history = _history(40, 4000)
    cloud = "claude-sonnet-4-5-20250929"
    with patch("app.routers.chat.offered_providers",
               return_value={"ollama", "anthropic"}):
        sink = _send(client, admin_headers, history, "s-window-cloud", model=cloud)
    sent = sink["first"]
    assert sent[1:] == [*history, {"role": "user", "content": PROMPT}]
    assert _trims(sink) == []
