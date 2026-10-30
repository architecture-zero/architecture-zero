"""A message that cannot fit a bounded window is refused before the call
(2026-10-29).

The history trim (_fit_history_to_window) never cuts the current message, and
the input bound admits about 50000 tokens of prompt (CHAT_MAX_INPUT_CHARS,
200000 characters at four a token). So one long paste on the local Ollama
lane, whose window is num_ctx (providers.OLLAMA_NUM_CTX, 32768 tokens by
default), still overflowed it with the whole history gone - and Ollama cuts
an over-long prompt from the FRONT, the system prompt and its safety rules
first.

What these pin, on that lane: a turn whose system prompt, current message and
reply room pass the window is refused with the size refusal's 413 and one
fixed sentence, before any row is stored and before any provider call - the
client takes the draft back, as it does for the other size refusals. One log
line says so, with counts and no text. CONTROL: a long message that fits is
sent whole, as before; a cloud lane sends the same long message as before.
"""
import json
from unittest.mock import patch

from app.providers import OLLAMA_NUM_CTX

LOCAL = "test-model"   # no vendor prefix and no namespace: the Ollama lane
REPLY_TOKENS = 4096    # a signed-in turn's max_tokens
SENTENCE = ("Message too long for this model: with its instructions and room "
            "for the reply, it does not fit the model's context window. "
            "Shorten the message, or choose a model with a larger window.")


def _words(tokens):
    """About `tokens` tokens of plain text at the chat's four characters a
    token."""
    return ("word " * (tokens * 4 // 5 + 1))[:tokens * 4]


def _send(client, headers, prompt, session, model=LOCAL, history=None):
    sink: dict = {"calls": 0, "saved": [], "events": []}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        sink["calls"] += 1
        sink["sent"] = [dict(m) for m in messages]
        yield {"type": "text", "text": "done"}

    from app.routers import chat as chat_mod
    real_save, real_log = chat_mod.save_message, chat_mod.log

    def _save(*a, **k):
        sink["saved"].append(a[1] if len(a) > 1 else k.get("role"))
        return real_save(*a, **k)

    def _log(event, **fields):
        sink["events"].append((event, fields))
        return real_log(event, **fields)

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.save_message", side_effect=_save), \
         patch("app.routers.chat.log", side_effect=_log):
        r = client.post("/api/chat", headers=headers, json={
            "prompt": prompt, "model": model, "use_rag": False,
            "session_id": session, "history": history or []})
    sink["response"] = r
    return sink


# -- FIX ----------------------------------------------------------------------

def test_a_message_past_the_window_with_the_reply_is_refused_uncalled(
        client, admin_headers):
    """30000 tokens: under the window alone, past it with the system prompt
    and the reply's 4096."""
    prompt = _words(30000)
    assert len(prompt) // 4 < OLLAMA_NUM_CTX < len(prompt) // 4 + REPLY_TOKENS
    sink = _send(client, admin_headers, prompt, "s-over-window")
    r = sink["response"]
    assert r.status_code == 413, r.text
    assert r.json()["detail"] == SENTENCE
    assert sink["calls"] == 0
    assert sink["saved"] == []
    stored = client.get("/api/history/s-over-window", headers=admin_headers)
    assert stored.json()["messages"] == []
    refused = [f for e, f in sink["events"] if e == "chat_request_refused"]
    assert len(refused) == 1, sink["events"]
    assert refused[0]["reason"] == "window"
    assert refused[0]["window"] == OLLAMA_NUM_CTX
    assert "word" not in json.dumps(refused[0])


def test_a_long_history_does_not_save_an_overlong_message(client, admin_headers):
    """The history all goes and the message still does not fit: refused."""
    history = [{"role": "user", "content": "earlier question"},
               {"role": "assistant", "content": "earlier answer"}]
    sink = _send(client, admin_headers, _words(31000), "s-over-window-hist",
                 history=history)
    assert sink["response"].status_code == 413
    assert sink["response"].json()["detail"] == SENTENCE
    assert sink["calls"] == 0 and sink["saved"] == []


# -- CONTROL ------------------------------------------------------------------

def test_a_long_message_that_fits_is_sent_whole(client, admin_headers):
    prompt = _words(20000)
    sink = _send(client, admin_headers, prompt, "s-fits-window")
    r = sink["response"]
    assert r.status_code == 200, r.text
    assert "done" in r.text
    assert sink["calls"] == 1
    assert sink["sent"][-1] == {"role": "user", "content": prompt}
    assert sink["saved"] == ["user", "assistant"]


def test_a_cloud_lane_sends_the_same_long_message(client, admin_headers):
    prompt = _words(30000)
    with patch("app.routers.chat.offered_providers",
               return_value={"ollama", "anthropic"}):
        sink = _send(client, admin_headers, prompt, "s-over-window-cloud",
                     model="claude-sonnet-4-5-20250929")
    r = sink["response"]
    assert r.status_code == 200, r.text
    assert sink["calls"] == 1
    assert sink["sent"][-1] == {"role": "user", "content": prompt}
