"""A turn's tool room is sized from the model's window (2026-10-29).

The history trim (_fit_history_to_window) reserves the turn's whole tool
budget before the history - agent.TOOL_RESULTS_TURN_MAX_CHARS, 48000
characters, 12000 tokens, about 17000 tokens with the reply's room, the tool
definitions and the rounds' own messages. On an 8192-token model that is more
than the window, so with tools on every turn sent NO history and the trim's
notice rode every turn - and the tool loop still let its results grow to the
whole 48000 characters, past the window.

What these pin: on a small window the turn's tool budget is a share of the
room the window leaves after the system prompt, the current message, the
reply, the tool definitions and the rounds' messages - two thirds of it, the
split the configured budget's own arithmetic makes on a 32768-token window
(12000 tool tokens beside a 6000-token history) - so some history is kept;
the tool loop's TurnToolBudget takes that same figure, so the results fit
what the trim reserved and the whole turn fits the window. CONTROL: a 32768
or larger window keeps the configured budget, also on a turn as heavy as that
arithmetic's own; a lane with no known window keeps it too.
"""
import json
import re
from unittest.mock import patch

import pytest

from app import agent, providers
from app.routers import chat as chat_mod
from tests.test_agent_tool_results_budget import _big_files, _tokens, _ws

REPLY_TOKENS = 4096   # a signed-in turn's max_tokens; it shares the window
PROMPT = "Read the logs."
LOCAL = "test-model"   # no vendor prefix and no namespace: the Ollama lane
BUDGET_OF = re.compile(r"cut at this turn's tool budget of (\d+) characters")


def _history(n, size):
    """n alternating messages of `size` characters, numbered."""
    out = []
    for i in range(n):
        head = f"[message {i:03d}] "
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": head + "x" * (size - len(head))})
    return out


def _send(client, headers, history, session, rounds, prompt=PROMPT, model=LOCAL):
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

    real_log = chat_mod.log

    def _log(event, **fields):
        sink["events"].append((event, fields))
        return real_log(event, **fields)

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.log", side_effect=_log):
        r = client.post("/api/chat", headers=headers, json={
            "prompt": prompt, "model": model, "use_rag": False,
            "session_id": session, "history": history})
    assert r.status_code == 200, r.text
    assert "done" in r.text
    sink["trims"] = [f for e, f in sink["events"] if e == "chat_history_trimmed"]
    return sink


def _kept(sent):
    """The history messages a turn sent (each starts with its number)."""
    return [m for m in sent[1:] if m.get("role") in ("user", "assistant")
            and m.get("content", "").startswith("[message")]


def _tool_budget(sent):
    """The turn's tool budget, as the tool loop's own cut marker states it."""
    for m in sent:
        if m.get("role") == "tool":
            found = BUDGET_OF.search(m["content"])
            if found:
                return int(found.group(1))
    raise AssertionError("no tool result was cut at the turn's budget")


def _reserved(budget_chars, tools):
    """What the history trim reserves for a tools-on turn with that budget."""
    return (REPLY_TOKENS + (budget_chars + len(json.dumps(tools))) // 4
            + chat_mod._TOOL_ROUNDS_ROOM_TOKENS)


# -- FIX ----------------------------------------------------------------------

def test_a_small_window_with_tools_keeps_some_history(
        client, admin_headers, monkeypatch, tmp_path):
    """An 8192-token window, tools on, a long history, two rounds of full
    reads: history is kept, and the results fit the budget the trim
    reserved, so the whole turn fits the window."""
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 8192)
    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 2)
    history = _history(40, 400)          # about 4000 tokens
    sink = _send(client, admin_headers, history, "s-room-8k", rounds=2)
    assert sink["tools"]
    sent = sink["last"]
    # The system prompt first and whole; some history kept, the newest, in
    # order - not none.
    assert sent[0] == sink["first"][0] and sent[0]["role"] == "system"
    kept = _kept(sent)
    assert 0 < len(kept) < len(history), len(kept)
    assert kept == history[-len(kept):]
    # The tool loop's budget is the figure the trim reserved for it, and it
    # is smaller than the configured one.
    budget = _tool_budget(sent)
    assert budget < agent.TOOL_RESULTS_TURN_MAX_CHARS
    assert len(sink["trims"]) == 1
    assert sink["trims"][0]["reserved"] == _reserved(budget, sink["tools"])
    tool_msgs = [m["content"] for m in sent if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert sum(len(c) for c in tool_msgs if c != agent.TOOL_BUDGET_SPENT) <= budget
    # The history's share is a third of the room against the tools' two
    # thirds - half the tool budget - less at most one message, since the
    # trim drops whole messages: a floor, not a scrap.
    one = len(history[0]["content"]) // 4
    assert _tokens(kept) >= budget // 4 // 2 - one, (_tokens(kept), budget)
    # The whole turn - history, both rounds, the reply - fits the window.
    definitions = len(json.dumps(sink["tools"])) // 4
    assert _tokens(sent) + definitions + REPLY_TOKENS <= 8192, _tokens(sent)


# -- CONTROL ------------------------------------------------------------------

@pytest.mark.parametrize("window", [32768, 131072])
def test_a_large_window_keeps_the_configured_budget(
        client, admin_headers, monkeypatch, tmp_path, window):
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", window)
    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 5)
    history = _history(4, 300)
    sink = _send(client, admin_headers, history, f"s-room-{window}", rounds=5)
    sent = sink["last"]
    assert _tool_budget(sent) == agent.TOOL_RESULTS_TURN_MAX_CHARS
    assert _kept(sent) == history


def test_a_heavy_turn_on_32k_keeps_the_configured_budget(
        client, admin_headers, monkeypatch, tmp_path):
    """The configured budget's own arithmetic (agent.py): a 3000-token system
    prompt, a 1700-token CONTEXT block and a 5000-token peer block - 9700
    tokens - on a 32768-token window leave 12000 tokens for tools beside a
    6000-token history. A turn of 9500 such tokens keeps the whole budget
    (half the room, not two thirds, would cut it to about 9100 tokens), and
    the history keeps its 6000."""
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 32768)
    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 5)
    # Measure the system prompt this turn sends, then pad the message so the
    # two together are 9500 tokens.
    probe = _send(client, admin_headers, [], "s-room-heavy-probe", rounds=0)
    system_tokens = len(probe["first"][0]["content"]) // 4
    message = "word " * ((9500 - system_tokens) * 4 // 5)
    history = _history(80, 400)          # about 8000 tokens: past its 6000
    sink = _send(client, admin_headers, history, "s-room-heavy", rounds=5,
                 prompt=message)
    sent = sink["last"]
    budget = _tool_budget(sent)
    assert budget == agent.TOOL_RESULTS_TURN_MAX_CHARS
    assert sink["trims"][0]["reserved"] == _reserved(budget, sink["tools"])
    assert _tokens(_kept(sent)) >= 5500


def test_a_lane_with_no_known_window_keeps_the_configured_budget(
        client, admin_headers, monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 5)
    history = _history(40, 400)
    cloud = "claude-sonnet-4-5-20250929"
    with patch("app.routers.chat.offered_providers",
               return_value={"ollama", "anthropic"}):
        sink = _send(client, admin_headers, history, "s-room-cloud", rounds=5,
                     model=cloud)
    sent = sink["last"]
    assert _tool_budget(sent) == agent.TOOL_RESULTS_TURN_MAX_CHARS
    assert _kept(sent) == history
    assert sink["trims"] == []
