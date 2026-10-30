"""OpenAI's own API is sent max_completion_tokens; other compatible hosts keep
max_tokens (2026-10-29).

OpenAI's reasoning models (the o-series, gpt-5) refuse a request that carries
max_tokens and require max_completion_tokens, which OpenAI's current chat
models also accept. Every other host that speaks the dialect (Groq, Mistral,
xAI, DeepSeek, Gemini's compatible endpoint, a self-hosted server) takes
max_tokens. The adapter sent max_tokens to every host, so a reasoning model on
OpenAI's own API answered every turn with a 400.

What these pin: the field is decided by the request's base URL, one helper
(providers._cap_field); a request to api.openai.com carries
max_completion_tokens and no max_tokens - the provider-level stream and a chat
turn through the real route. CONTROL: a request to any other compatible host,
and to the openai entry pointed at another host, carries max_tokens as before,
and a host that merely starts with OpenAI's name is another host.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app import providers
from app.providers import stream_chat_events

_MSGS = [{"role": "user", "content": "what's next?"}]


def _stream_resp():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = [
        b'data: {"choices": [{"delta": {"content": "hi"}}]}', b"data: [DONE]"]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _sent(model, max_tokens=321):
    """The URL and payload one streamed request to `model` carries."""
    with patch("app.providers.req.post", return_value=_stream_resp()) as post:
        events = list(stream_chat_events(_MSGS, model, max_tokens=max_tokens))
    assert events == [{"type": "text", "text": "hi"}]
    return post.call_args.args[0], post.call_args.kwargs["json"]


@pytest.fixture(autouse=True)
def _default_bases(monkeypatch):
    """Each registry entry on its own default base for these tests."""
    for name in providers.OPENAI_COMPAT:
        monkeypatch.delenv(f"{name.upper()}_BASE_URL", raising=False)
    real = providers._get_runtime
    monkeypatch.setattr(providers, "_get_runtime",
                        lambda db_key, env_key, default="": (
                            default if db_key.endswith("_base_url")
                            else real(db_key, env_key, default)))


# -- FIX ----------------------------------------------------------------------

def test_openais_own_api_is_sent_max_completion_tokens():
    url, payload = _sent("gpt-4o")
    assert url == "https://api.openai.com/v1/chat/completions"
    assert payload["max_completion_tokens"] == 321
    assert "max_tokens" not in payload, payload


def test_a_reasoning_model_on_openai_is_sent_max_completion_tokens():
    url, payload = _sent("o3-mini", max_tokens=4096)
    assert url.startswith("https://api.openai.com/")
    assert payload["max_completion_tokens"] == 4096
    assert "max_tokens" not in payload


def test_the_helper_reads_the_host():
    assert providers._cap_field("https://api.openai.com/v1") == "max_completion_tokens"
    assert providers._cap_field("https://API.OpenAI.com:443/v1/") == "max_completion_tokens"


def test_a_chat_turn_to_openai_carries_max_completion_tokens(client, admin_headers):
    """The chat's own turn through the real route: a signed-in turn's cap
    (4096) rides as max_completion_tokens to OpenAI's API."""
    with patch("app.routers.chat.offered_providers",
               return_value={"ollama", "openai"}), \
         patch("app.providers.req.post", return_value=_stream_resp()) as post:
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": "hello", "model": "gpt-4o", "use_rag": False,
            "session_id": "s-cap-field-openai"})
    assert r.status_code == 200, r.text
    sent = [c.kwargs["json"] for c in post.call_args_list
            if str(c.args[0]).endswith("/chat/completions")]
    assert sent, post.call_args_list
    for payload in sent:
        assert payload["max_completion_tokens"] == 4096, json.dumps(payload)[:200]
        assert "max_tokens" not in payload


# -- CONTROL ------------------------------------------------------------------

@pytest.mark.parametrize("model, host", [
    ("groq:llama-3.3-70b-versatile", "https://api.groq.com/"),
    ("mistral-large-latest", "https://api.mistral.ai/"),
    ("grok-4", "https://api.x.ai/"),
    ("deepseek-chat", "https://api.deepseek.com/"),
    ("gemini-3.6-flash", "https://generativelanguage.googleapis.com/"),
])
def test_every_other_compatible_host_keeps_max_tokens(model, host):
    url, payload = _sent(model)
    assert url.startswith(host), url
    assert payload["max_tokens"] == 321
    assert "max_completion_tokens" not in payload


def test_the_openai_entry_pointed_at_another_host_keeps_max_tokens(monkeypatch):
    """The field follows the host the request goes to, not the provider's
    name: an operator's own compatible server behind the openai entry."""
    monkeypatch.setattr(providers, "_compat_base",
                        lambda provider: "http://compat.internal:8000/v1")
    url, payload = _sent("gpt-4o")
    assert url == "http://compat.internal:8000/v1/chat/completions"
    assert payload["max_tokens"] == 321
    assert "max_completion_tokens" not in payload


def test_a_host_that_only_starts_with_openais_name_is_another_host():
    assert providers._cap_field("https://api.openai.com.example.test/v1") == "max_tokens"
    assert providers._cap_field("http://localhost:11434/v1") == "max_tokens"
    assert providers._cap_field("") == "max_tokens"
