"""The model catalog: family aliases that follow the newest model, pins that
do not, and a picker that lists what is current rather than every version.

A setting on "claude-opus-latest" must send the newest Opus the key lists,
so a new release moves it with no edit; a concrete id must pass through
untouched (a pin); the eval JUDGE must be stored as a pin even when a family
is picked for it - the ruler never moves on its own - while the eval writer
may follow a family like any other slot, and a run records the version it used.
"""
from unittest.mock import patch

import pytest

from app import model_catalog
from app.model_catalog import (family_of, resolve, anthropic_picker_models,
                               trim_to_newest, FAMILY_FALLBACK)
from app.providers import _resolve_model

LIST = [
    {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1", "created_at": "2026-08-01T00:00:00Z"},
    {"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5", "created_at": "2026-07-15T00:00:00Z"},
    {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "created_at": "2026-07-01T00:00:00Z"},
    {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8", "created_at": "2026-03-01T00:00:00Z"},
    {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6", "created_at": "2026-01-01T00:00:00Z"},
    {"id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5", "created_at": "2025-10-01T00:00:00Z"},
    {"id": "claude-3-5-sonnet-20241022", "display_name": "Claude Sonnet 3.5", "created_at": "2024-10-22T00:00:00Z"},
]


@pytest.fixture
def catalog(monkeypatch):
    """The live list served from a variable, and no memory of earlier answers."""
    state = {"models": list(LIST)}
    monkeypatch.setattr(model_catalog, "_anthropic_list", lambda: state["models"])
    monkeypatch.setattr(model_catalog, "_last_resolved", {})
    return state


def test_the_family_of_an_id_reads_both_naming_shapes():
    assert family_of("claude-opus-5-5") == "opus"
    assert family_of("claude-haiku-4-5-20251001") == "haiku"
    assert family_of("claude-3-5-sonnet-20241022") == "sonnet"
    assert family_of("claude-opus-latest") == "opus"
    assert family_of("gpt-4o") == "" and family_of("") == "" and family_of("qwen3:8b") == ""


def test_an_alias_sends_the_newest_model_of_its_family(catalog):
    assert resolve("claude-opus-latest") == "claude-opus-5-5"
    assert resolve("claude-sonnet-latest") == "claude-sonnet-5-5"
    assert resolve("claude-haiku-latest") == "claude-haiku-4-5-20251001"
    assert resolve("claude-fable-latest") == "claude-fable-5-1"


def test_a_new_release_moves_the_alias_with_no_edit_and_says_so(catalog, monkeypatch):
    seen = []
    monkeypatch.setattr("app.logger.log", lambda event, **f: seen.append((event, f)))
    assert resolve("claude-opus-latest") == "claude-opus-5-5"
    catalog["models"] = [{"id": "claude-opus-5-6", "display_name": "Claude Opus 5.6",
                          "created_at": "2026-11-01T00:00:00Z"}] + LIST
    assert resolve("claude-opus-latest") == "claude-opus-5-6"
    assert ("model_family_moved", {"family": "opus", "from_model": "claude-opus-5-5",
                                   "to_model": "claude-opus-5-6"}) in seen


def test_the_eval_job_resolves_its_writer_before_anything_reads_it():
    # scripts/eval_retrieval.py hands its model straight to the job, so the
    # job resolves it itself - before the first use (supports_tools).
    import inspect
    from app import eval_runner
    src = inspect.getsource(eval_runner._run_eval_job)
    first = src.index("model = model_catalog.resolve(model)")
    assert first < src.index("supports_tools(model)")


def test_a_pin_and_every_other_value_pass_through_unchanged(catalog):
    for value in ("claude-opus-4-8", "claude-sonnet-4-6", "gemini-3.6-flash", "qwen3:8b",
                  "groq:llama-3.3-70b-versatile", "mistral-large-latest", ""):
        assert resolve(value) == value


def test_with_no_list_an_alias_keeps_its_last_answer_then_the_fallback(catalog):
    assert resolve("claude-sonnet-latest") == "claude-sonnet-5-5"
    catalog["models"] = []
    assert resolve("claude-sonnet-latest") == "claude-sonnet-5-5"            # last answer
    assert resolve("claude-haiku-latest") == FAMILY_FALLBACK["haiku"]       # never resolved here
    assert resolve("claude-nosuch-latest") == "claude-nosuch-latest"        # the provider refuses it


def _wait_for_refresh():
    import threading
    for t in threading.enumerate():
        if t.name == "model-catalog-refresh":
            t.join(timeout=5)


def test_a_stale_list_answers_at_once_and_refreshes_behind(monkeypatch):
    import time as _t
    stale = [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"}]
    fresh = [{"id": "claude-opus-5-6", "display_name": "Claude Opus 5.6"}]
    monkeypatch.setattr(model_catalog, "_list_cache",
                        {"ts": _t.time() - 7200, "ttl": 3600, "models": stale})
    monkeypatch.setattr(model_catalog, "_fetch_list", lambda: fresh)
    assert model_catalog._anthropic_list() is stale          # no wait for the network
    _wait_for_refresh()
    assert model_catalog._anthropic_list() == fresh


def test_a_failed_refresh_keeps_the_stale_list_and_asks_again_sooner(monkeypatch):
    import time as _t
    stale = [{"id": "claude-opus-5-5"}]
    monkeypatch.setattr(model_catalog, "_list_cache",
                        {"ts": _t.time() - 7200, "ttl": 3600, "models": stale})
    monkeypatch.setattr(model_catalog, "_fetch_list", lambda: [])
    model_catalog._anthropic_list()
    _wait_for_refresh()
    assert model_catalog._list_cache["models"] is stale
    assert model_catalog._list_cache["ttl"] == model_catalog.FAILURE_CACHE_SECONDS


def test_with_no_list_yet_the_first_read_is_inline(monkeypatch):
    monkeypatch.setattr(model_catalog, "_list_cache", {"ts": 0.0, "ttl": 0.0, "models": None})
    monkeypatch.setattr(model_catalog, "_fetch_list", lambda: [{"id": "claude-haiku-4-5"}])
    assert model_catalog._anthropic_list() == [{"id": "claude-haiku-4-5"}]


def test_the_dispatch_resolver_sends_the_concrete_id(catalog):
    assert _resolve_model("claude-opus-latest") == ("anthropic", "claude-opus-5-5")


def test_the_picker_lists_one_entry_per_family_newest_first_in_tier_order(catalog):
    models = anthropic_picker_models(lambda mid: "B:" + mid)
    assert [m["value"] for m in models] == ["claude-opus-latest", "claude-sonnet-latest",
                                            "claude-haiku-latest", "claude-fable-latest"]
    assert models[0]["label"] == "Claude Opus - newest (now Opus 5.5)"
    assert models[0]["badge"] == "B:claude-opus-5-5"
    assert all("4-8" not in m["value"] and "4-6" not in m["value"] for m in models)


def test_with_no_list_the_picker_offers_the_fallback_families(catalog):
    catalog["models"] = []
    assert [m["value"] for m in anthropic_picker_models(lambda mid: "")] == \
        [f"claude-{f}-latest" for f in FAMILY_FALLBACK]


def _vals(*ids):
    return [{"value": i, "label": i, "badge": "x"} for i in ids]


def test_other_providers_keep_the_newest_stable_model_of_each_family():
    kept = trim_to_newest(_vals(
        "gemini-2.0-flash-001", "gemini-2.5-flash", "gemini-3.6-flash", "gemini-2.5-flash-lite",
        "gemini-3.6-pro-preview-06-05", "gemini-2.5-pro", "gemini-embedding-001",
        "gemini-2.5-flash-image", "gemini-2.5-flash-preview-tts"))
    assert [m["value"] for m in kept] == ["gemini-3.6-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]


def test_a_preview_stays_when_its_family_has_nothing_stable():
    kept = trim_to_newest(_vals("gemini-4.0-ultra-preview"))
    assert [m["value"] for m in kept] == ["gemini-4.0-ultra-preview"]


def test_a_providers_own_latest_alias_is_the_familys_entry():
    kept = trim_to_newest(_vals("mistral-large-2411", "mistral-large-latest", "mistral-small-2503",
                                "mistral-embed", "mistral-moderation-latest"))
    assert [m["value"] for m in kept] == ["mistral-large-latest", "mistral-small-2503"]


def test_creation_time_decides_when_the_provider_gives_one():
    models = _vals("gpt-4o", "gpt-4.1", "gpt-5", "gpt-4o-mini", "gpt-5-mini", "gpt-4o-transcribe")
    created = {"gpt-4o": 1, "gpt-4.1": 2, "gpt-5": 3, "gpt-4o-mini": 1, "gpt-5-mini": 3}
    assert [m["value"] for m in trim_to_newest(models, created)] == ["gpt-5", "gpt-5-mini"]


def test_namespaced_ids_parse_after_the_namespace_and_sizes_stay_apart():
    kept = trim_to_newest(_vals("groq:llama-3.1-8b-instant", "groq:llama-3.3-70b-versatile",
                                "groq:llama-3.1-70b-versatile", "groq:whisper-large-v3"))
    assert [m["value"] for m in kept] == ["groq:llama-3.1-8b-instant", "groq:llama-3.3-70b-versatile"]


def test_every_version_lists_each_claude_model_newest_first_as_a_pin(catalog):
    models = model_catalog.anthropic_all_versions(lambda mid: "")
    assert [m["value"] for m in models] == [
        "claude-fable-5-1", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-4-8",
        "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-3-5-sonnet-20241022"]
    assert models[2]["label"] == "Claude Opus 5.5"


# -- The routes ---------------------------------------------------------------

def test_the_models_route_lists_families_not_versions(client, admin_headers, catalog):
    with patch("app.routers.settings.offered_providers", return_value={"anthropic"}):
        groups = client.get("/api/models", headers=admin_headers).json()["groups"]
    values = [m["value"] for m in groups[0]["models"]]
    assert values[:3] == ["claude-opus-latest", "claude-sonnet-latest", "claude-haiku-latest"]
    assert "claude-opus-4-8" not in values


def test_the_every_version_switch_adds_the_pins_and_the_untrimmed_lists(
        client, admin_headers, catalog, monkeypatch):
    import app.routers.settings as s
    from unittest.mock import MagicMock
    live = MagicMock()
    live.raise_for_status.return_value = None
    live.json.return_value = {"data": [{"id": "models/gemini-2.5-flash"},
                                       {"id": "models/gemini-3.6-flash"}]}
    monkeypatch.setattr(s, "offered_providers", lambda: {"anthropic", "gemini"})
    s._compat_models_cache.clear()
    with patch("app.routers.settings.requests.get", return_value=live):
        current = client.get("/api/models", headers=admin_headers).json()["groups"]
        every = client.get("/api/models?all_versions=true", headers=admin_headers).json()["groups"]
    s._compat_models_cache.clear()
    assert [g["label"] for g in current] == ["Anthropic", "Gemini"]
    assert [m["value"] for m in current[1]["models"]] == ["gemini-3.6-flash"]
    labels = [g["label"] for g in every]
    assert labels == ["Anthropic", "Anthropic - every version (pins)", "Gemini"]
    assert "claude-opus-4-8" in [m["value"] for m in every[1]["models"]]
    assert [m["value"] for m in every[2]["models"]] == ["gemini-2.5-flash", "gemini-3.6-flash"]


def test_a_chat_on_an_alias_answers_with_and_records_the_concrete_version(client, admin_headers, catalog):
    def _cfg(key, default=None):
        if key == "chat_model":
            return "claude-opus-latest"
        return default
    seen = {}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        seen["model"] = model
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.get_config", side_effect=_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_stream):
        r = client.post("/api/chat", json={"prompt": "Hi"}, headers=admin_headers)
    assert r.status_code == 200 and seen["model"] == "claude-opus-5-5"


def test_the_judge_pins_a_picked_family_and_the_writer_follows_one(client, admin_headers, catalog):
    from app.config import get_config
    r = client.patch("/api/admin/model-config", headers=admin_headers,
                     json={"default": "claude-opus-latest", "eval_judge": "claude-sonnet-latest",
                           "eval_writer": "claude-haiku-latest"})
    assert r.status_code == 200
    assert get_config("default_model", "") == "claude-opus-latest"          # follows its family
    assert get_config("eval_judge_model", "") == "claude-sonnet-5-5"        # the ruler: pinned
    assert get_config("eval_answer_model", "") == "claude-haiku-latest"     # rolls
    body = r.json()
    assert body["default"]["value"] == "claude-opus-latest"
    assert body["default"]["resolved"] == "claude-opus-5-5"
    assert body["eval_writer"]["resolved"] == "claude-haiku-4-5-20251001"
    r = client.patch("/api/admin/config", headers=admin_headers,
                     json={"eval_judge_model": "claude-opus-latest",
                           "eval_answer_model": "claude-sonnet-latest"})
    assert r.status_code == 200
    assert get_config("eval_judge_model", "") == "claude-opus-5-5"
    assert get_config("eval_answer_model", "") == "claude-sonnet-latest"
    client.patch("/api/admin/model-config", headers=admin_headers,
                 json={"default": "", "eval_judge": "", "eval_writer": ""})


def test_the_chat_config_names_the_version_that_answers(client, admin_headers, catalog):
    with patch("app.routers.system.get_config",
               side_effect=lambda k, d=None: "claude-sonnet-latest" if k == "chat_model" else d):
        d = client.get("/api/config", headers=admin_headers).json()
    assert d["chat_model_effective"] == "claude-sonnet-latest"
    assert d["chat_model_resolved"] == "claude-sonnet-5-5"
