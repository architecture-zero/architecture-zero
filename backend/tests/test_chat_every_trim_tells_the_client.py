"""Every history trim tells the client (2026-10-29).

The client learns that older messages are out of the model's view from one
stream event, context_warning (the banner "This conversation is getting long
- responses may be less accurate"), and the chat sent it only when the
history passed MAX_CONTEXT_TOKENS (6000). The window trim
(_fit_history_to_window) can drop turns from a shorter history - a long
message, or the tool room, leaves less for it - and then the model forgot
them with nothing shown.

What these pin: a trim of a history under MAX_CONTEXT_TOKENS sends
context_warning, once, before the answer; a trim of a long history sends it
once, not twice. CONTROL: no trim, no notice; a long history on a cloud lane
gets the one notice it got before.
"""
from unittest.mock import patch

from app.providers import OLLAMA_NUM_CTX
from app.runtime_config import MAX_CONTEXT_TOKENS

LOCAL = "test-model"   # no vendor prefix and no namespace: the Ollama lane
WARNING = 'data: {"context_warning": true}'


def _history(n, size):
    out = []
    for i in range(n):
        head = f"[message {i:03d}] "
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": head + "x" * (size - len(head))})
    return out


def _words(tokens):
    return ("word " * (tokens * 4 // 5 + 1))[:tokens * 4]


def _send(client, headers, prompt, history, session, model=LOCAL):
    sink: dict = {"events": []}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        sink["sent"] = [dict(m) for m in messages]
        yield {"type": "text", "text": "done"}

    from app.routers import chat as chat_mod
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
    sink["lines"] = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
    sink["trims"] = [f for e, f in sink["events"] if e == "chat_history_trimmed"]
    return sink


def _tokens(messages):
    return sum(len(m["content"]) for m in messages) // 4


# -- FIX ----------------------------------------------------------------------

def test_a_trim_of_a_short_history_sends_the_notice(client, admin_headers):
    """A 5000-token history beside a 24000-token message: under the 6000
    the old notice keyed on, and still trimmed to fit the window."""
    history = _history(10, 2000)
    assert _tokens(history) <= MAX_CONTEXT_TOKENS
    sink = _send(client, admin_headers, _words(24000), history, "s-trim-short")
    assert len(sink["trims"]) == 1, sink["trims"]
    assert sink["trims"][0]["dropped"] > 0
    assert sink["lines"].count(WARNING) == 1, sink["lines"][:5]
    first_token = next(i for i, ln in enumerate(sink["lines"]) if '"token"' in ln)
    assert sink["lines"].index(WARNING) < first_token


def test_a_trim_of_a_long_history_sends_the_notice_once(client, admin_headers):
    history = _history(40, 4000)
    assert _tokens(history) > OLLAMA_NUM_CTX
    sink = _send(client, admin_headers, "What did we decide?", history,
                 "s-trim-long")
    assert len(sink["trims"]) == 1
    assert sink["lines"].count(WARNING) == 1, sink["lines"][:5]


# -- CONTROL ------------------------------------------------------------------

def test_no_trim_no_notice(client, admin_headers):
    history = _history(4, 300)
    sink = _send(client, admin_headers, "What did we decide?", history,
                 "s-trim-none")
    assert sink["trims"] == []
    assert WARNING not in sink["lines"]
    assert sink["sent"][1:-1] == history


def test_a_long_history_on_a_cloud_lane_gets_its_one_notice(
        client, admin_headers):
    history = _history(40, 4000)
    with patch("app.routers.chat.offered_providers",
               return_value={"ollama", "anthropic"}):
        sink = _send(client, admin_headers, "What did we decide?", history,
                     "s-trim-cloud", model="claude-sonnet-4-5-20250929")
    assert sink["trims"] == []
    assert sink["lines"].count(WARNING) == 1
    assert sink["sent"][1:-1] == history
