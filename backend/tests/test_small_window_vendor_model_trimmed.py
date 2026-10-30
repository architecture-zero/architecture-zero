"""A small-window vendor model is trimmed as the bounded local lane is
(2026-10-29).

Only the local Ollama lane had a window this code knew
(providers.bounded_window_tokens), so only it trimmed a long conversation to
fit. Some vendor models have small windows - Groq lists models of 8192
tokens, Mistral its legacy open-* ids at 32768 - and the input bound admits
about 50000 tokens, so a long conversation there was refused by the vendor
with an error, and the visitor got the chat's fixed failure sentence instead
of an answer.

Where the window comes from: the vendor's own /models list, the one the
picker reads (routers/settings.py _fetch_compat_models), which declares it
per model - Groq as `context_window`, Mistral as `max_context_length`.
Nothing is hard-coded: a model the list does not give a window for (OpenAI's
list gives none, nor do the others) is unknown, and an unknown window is
today's behavior.

What these pin: after the list is read, a listed small window is the
model's bound - a long history is trimmed oldest first to fit it, the reply's
room kept, the client told; a single message too long for it is refused
before the call, as on the local lane. CONTROL: a listed large window sends
the whole history as before; an unlisted vendor model sends it as before;
before the list is read nothing is known.
"""
from unittest.mock import MagicMock, patch

import pytest

from app import model_catalog, providers
from app.routers import settings as settings_mod

REPLY_TOKENS = 4096
# An 8192-token window's reply room: a quarter of it (chat._reply_room,
# 2026-10-29), the cap the request carries and the room the trim keeps.
SMALL_REPLY_TOKENS = 2048
PROMPT = "What did we decide about the release?"
WARNING = 'data: {"context_warning": true}'


@pytest.fixture(autouse=True)
def _fresh_lists(monkeypatch):
    monkeypatch.setattr(model_catalog, "_listed_windows", {}, raising=False)
    monkeypatch.setattr(settings_mod, "_compat_models_cache", {})


def _read_list(provider, rows):
    """One read of a vendor's /models list, as the picker reads it."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": rows}
    with patch.object(settings_mod.requests, "get", return_value=resp):
        return settings_mod._fetch_compat_models(provider)


def _history(n, size):
    out = []
    for i in range(n):
        head = f"[message {i:03d}] "
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": head + "x" * (size - len(head))})
    return out


def _tokens(messages):
    return sum(len(m.get("content", "")) for m in messages) // 4


def _send(client, headers, model, provider, history, prompt=PROMPT):
    sink: dict = {"calls": 0}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        sink["calls"] += 1
        sink["sent"] = [dict(m) for m in messages]
        yield {"type": "text", "text": "done"}

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.offered_providers",
               return_value={"ollama", provider}):
        r = client.post("/api/chat", headers=headers, json={
            "prompt": prompt, "model": model, "use_rag": False,
            "session_id": f"s-vendor-{model}", "history": history})
    sink["response"] = r
    sink["lines"] = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
    return sink


# -- FIX ----------------------------------------------------------------------

def test_the_list_gives_the_models_window():
    _read_list("groq", [{"id": "small-8k", "context_window": 8192},
                        {"id": "big-128k", "context_window": 131072}])
    _read_list("mistral", [{"id": "open-mistral-7b", "max_context_length": 32768}])
    assert providers.bounded_window_tokens("groq:small-8k") == 8192
    assert providers.bounded_window_tokens("groq:big-128k") == 131072
    assert providers.bounded_window_tokens("open-mistral-7b") == 32768


def test_a_small_window_vendor_model_trims_a_long_history(client, admin_headers):
    _read_list("groq", [{"id": "small-8k", "context_window": 8192}])
    history = _history(10, 2000)          # 5000 tokens: past 8192 with the rest
    sink = _send(client, admin_headers, "groq:small-8k", "groq", history)
    assert sink["response"].status_code == 200, sink["response"].text
    sent = sink["sent"]
    kept = sent[1:-1]
    assert sent[0]["role"] == "system"
    assert sent[-1] == {"role": "user", "content": PROMPT}
    assert 0 < len(kept) < len(history), len(kept)
    assert kept == history[-len(kept):]
    assert _tokens(sent) + SMALL_REPLY_TOKENS <= 8192, _tokens(sent)
    assert sink["lines"].count(WARNING) == 1


def test_a_legacy_mistral_window_trims_a_long_history(client, admin_headers):
    _read_list("mistral", [{"id": "open-mistral-7b", "max_context_length": 32768}])
    history = _history(40, 4000)          # 40000 tokens
    sink = _send(client, admin_headers, "open-mistral-7b", "mistral", history)
    sent = sink["sent"]
    kept = sent[1:-1]
    assert 0 < len(kept) < len(history)
    assert kept == history[-len(kept):]
    assert _tokens(sent) + REPLY_TOKENS <= 32768


def test_a_message_too_long_for_the_small_window_is_refused(client, admin_headers):
    _read_list("groq", [{"id": "small-8k", "context_window": 8192}])
    sink = _send(client, admin_headers, "groq:small-8k", "groq", [],
                 prompt="word " * 4800)       # 6000 tokens, past 8192 with the rest
    assert sink["response"].status_code == 413
    assert sink["calls"] == 0


# -- CONTROL ------------------------------------------------------------------

def test_nothing_is_known_before_the_list_is_read():
    assert providers.bounded_window_tokens("groq:small-8k") is None
    assert providers.bounded_window_tokens("open-mistral-7b") is None


def test_a_large_listed_window_sends_the_whole_history(client, admin_headers):
    _read_list("groq", [{"id": "big-128k", "context_window": 131072}])
    history = _history(40, 4000)
    sink = _send(client, admin_headers, "groq:big-128k", "groq", history)
    assert sink["sent"][1:-1] == history


def test_an_unlisted_vendor_window_sends_the_whole_history(client, admin_headers):
    """OpenAI's list gives no window: unknown, today's behavior."""
    _read_list("openai", [{"id": "gpt-4o", "created": 1}])
    assert providers.bounded_window_tokens("gpt-4o") is None
    history = _history(40, 4000)
    sink = _send(client, admin_headers, "gpt-4o", "openai", history)
    assert sink["sent"][1:-1] == history


def test_a_window_that_is_not_a_positive_number_is_unknown():
    _read_list("groq", [{"id": "odd", "context_window": "8192"},
                        {"id": "zero", "context_window": 0},
                        {"id": "flag", "context_window": True}])
    for mid in ("groq:odd", "groq:zero", "groq:flag"):
        assert providers.bounded_window_tokens(mid) is None, mid
