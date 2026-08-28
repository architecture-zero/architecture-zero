"""Provider-registry tests (multi-provider readiness).

The OPENAI_COMPAT registry turns "add a provider" into a registry entry + a
key: one OpenAI-dialect adapter serves OpenAI, Gemini, Mistral, Groq, xAI and
DeepSeek. These pin the two things that silently break users if they drift:
ROUTING (which provider a model id reaches, incl. the "provider:model"
namespace and the Ollama colon-tag passthrough) and WIRE SHAPE (the bare model
id, per-provider base URL, and per-provider Bearer key on the actual request).
"""
import json
from unittest.mock import MagicMock, patch

from app.providers import (OPENAI_COMPAT, _compat_base, _compat_headers,
                           _resolve_model, get_provider_config,
                           non_stream_tool_call)


# -- Routing ------------------------------------------------------------------

def test_prefix_routing_covers_every_registry_provider():
    assert _resolve_model("claude-sonnet-4-6") == ("anthropic", "claude-sonnet-4-6")
    assert _resolve_model("gpt-4o") == ("openai", "gpt-4o")
    assert _resolve_model("o3-mini") == ("openai", "o3-mini")
    assert _resolve_model("gemini-3.6-flash") == ("gemini", "gemini-3.6-flash")
    assert _resolve_model("mistral-large-latest") == ("mistral", "mistral-large-latest")
    assert _resolve_model("grok-4.5") == ("xai", "grok-4.5")
    assert _resolve_model("deepseek-v4-flash") == ("deepseek", "deepseek-v4-flash")


def test_ollama_colon_tags_pass_through_untouched():
    # Ollama ids contain colons ("name:tag") - the namespace parse must only
    # fire for KNOWN provider names, or every local model breaks.
    assert _resolve_model("qwen3:8b") == ("ollama", "qwen3:8b")
    assert _resolve_model("llama3.2:8b") == ("ollama", "llama3.2:8b")


def test_explicit_namespace_wins_and_strips():
    # Groq model names (llama/qwen families) have no unique prefix - the
    # explicit namespace is the only route to them.
    assert _resolve_model("groq:llama-3.3-70b-versatile") == \
        ("groq", "llama-3.3-70b-versatile")
    assert _resolve_model("ollama:gemini-fake:1b") == ("ollama", "gemini-fake:1b")
    assert _resolve_model("anthropic:claude-x") == ("anthropic", "claude-x")


def test_unknown_model_falls_back_to_ollama():
    assert _resolve_model("some-local-model") == ("ollama", "some-local-model")


# -- Per-provider base + key --------------------------------------------------

def test_compat_base_defaults_are_the_registry_entries():
    for name, entry in OPENAI_COMPAT.items():
        assert _compat_base(name) == entry["base"].rstrip("/")


def test_compat_base_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_BASE_URL", "http://proxy.local/v1/")
    assert _compat_base("gemini") == "http://proxy.local/v1"  # trailing / stripped


def test_compat_headers_use_the_providers_own_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    assert _compat_headers("gemini")["Authorization"] == "Bearer gem-key"
    assert _compat_headers("groq")["Authorization"] == "Bearer groq-key"


def test_provider_config_reports_configured_per_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    cfg = get_provider_config()
    assert cfg["gemini_configured"] is True
    assert cfg["mistral_configured"] is False
    for name in OPENAI_COMPAT:
        assert f"{name}_configured" in cfg


# -- Wire shape ---------------------------------------------------------------

_TOOLS = [{"type": "function",
           "function": {"name": "t", "description": "", "parameters": {}}}]


def _json_resp(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def test_gemini_request_hits_gemini_base_with_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    resp = _json_resp({"choices": [{"message": {"content": "ok"}}]})
    with patch("app.providers.req.post", return_value=resp) as post:
        non_stream_tool_call([{"role": "user", "content": "q"}],
                             "gemini-3.6-flash", _TOOLS)
    url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs["url"]
    assert url == ("https://generativelanguage.googleapis.com/v1beta/openai"
                   "/chat/completions")
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer gem-key"
    assert post.call_args.kwargs["json"]["model"] == "gemini-3.6-flash"


def test_namespaced_groq_request_sends_bare_model_id(monkeypatch):
    # The provider tag is OUR routing syntax - it must never reach the wire.
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    resp = _json_resp({"choices": [{"message": {"content": "ok"}}]})
    with patch("app.providers.req.post", return_value=resp) as post:
        non_stream_tool_call([{"role": "user", "content": "q"}],
                             "groq:llama-3.3-70b-versatile", _TOOLS)
    url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs["url"]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert post.call_args.kwargs["json"]["model"] == "llama-3.3-70b-versatile"


def test_gemini_stream_parses_openai_dialect(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    from app.providers import stream_chat_events
    lines = [
        b'data: ' + json.dumps(
            {"choices": [{"delta": {"content": "hel"}}]}).encode(),
        b'data: ' + json.dumps(
            {"choices": [{"delta": {"content": "lo"}}]}).encode(),
        b'data: [DONE]',
    ]
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = lines
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    with patch("app.providers.req.post", return_value=resp) as post:
        events = list(stream_chat_events(
            [{"role": "user", "content": "q"}], "gemini-3.6-flash"))
    assert post.call_args.kwargs["json"]["stream"] is True
    assert events == [{"type": "text", "text": "hel"},
                      {"type": "text", "text": "lo"}]


# -- API surface --------------------------------------------------------------

def test_models_endpoint_lists_keyed_provider_with_fallback(client, admin_headers, monkeypatch):
    # Key present + live /models unreachable -> the provider still appears,
    # served from the static fallback list (the picker must never be empty
    # for a keyed provider).
    # Both targets follow the code: _compat_models_cache is DEFINED in the
    # settings router now, and main no longer imports requests at all - so the
    # old "app.main.requests.get" would raise at patch setup rather than quietly
    # patching a module main does not read. Kept as a patch on the module object
    # rather than a scoped mock, deliberately: that global reach is also what
    # blanks runtime_config._ollama_get, which is why this test passes today.
    import app.routers.settings as m
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    m._compat_models_cache.clear()
    with patch("app.routers.settings.requests.get", side_effect=Exception("offline")):
        r = client.get("/api/models", headers=admin_headers)
    assert r.status_code == 200
    groups = {g["provider"]: g for g in r.json()["groups"]}
    assert "gemini" in groups
    values = [x["value"] for x in groups["gemini"]["models"]]
    assert "gemini-3.6-flash" in values
    assert "mistral" not in groups  # unkeyed providers stay dormant
    m._compat_models_cache.clear()


def test_settings_roundtrip_gemini_key(client, admin_headers):
    from app.config import set_config
    r = client.get("/api/settings", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["gemini_key_set"] is False
    r = client.put("/api/settings", headers=admin_headers,
                   json={"gemini_api_key": "gem-key-123"})
    assert r.status_code == 200
    assert r.json()["gemini_key_set"] is True
    # masked placeholder must not overwrite the stored key
    r = client.put("/api/settings", headers=admin_headers,
                   json={"gemini_api_key": "***"})
    assert r.json()["gemini_key_set"] is True
    set_config("gemini_api_key", "")  # don't leak state into other tests


def test_model_config_endpoint_still_resolves_after_the_settings_split(client, admin_headers):
    """/api/admin/model-config had NO coverage, and the split manifest wanted to
    move _model_config_dict into the settings router - where its two real
    callers, both admin routes, would have raised NameError at request time with
    a green suite to say otherwise.

    So this pins the seam rather than the feature: the handler resolves every
    name it closes over (_config_or_default and DEFAULT_MODEL now from
    runtime_config, EVAL_JUDGE_MODEL_DEFAULT and get_config still main's) and
    returns its real shape. It exists to fail when a later commit moves the
    helper away from its callers.
    """
    r = client.get("/api/admin/model-config", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("default", "chat", "eval_writer", "eval_judge"):
        assert key in body, key
        assert "value" in body[key] and "overridden" in body[key], key
    assert "same_family_warning" in body
