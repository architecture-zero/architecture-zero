"""A key the vendor refuses is not a provider the picker offers (2026-11-06).

The static fallback list in routers/settings.py is for an outage. Before this
change a /models read the vendor REFUSED - a deleted or revoked key - fell to
the same fallback, so the picker offered that provider's models, every one of
which could only fail, and the refusal itself never showed anywhere. Also
here: the chat picker's list drops Gemini's robot-control, computer-use and
image ids, which were reaching it.
"""
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

import app.routers.settings as s
from app import model_catalog


def _resp(status):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"data": [{"id": "gemini-flash-latest"}]}

    def _raise():
        if status >= 400:
            raise requests.HTTPError(str(status))
    r.raise_for_status.side_effect = _raise
    return r


@pytest.fixture(autouse=True)
def _clean_cache():
    s._compat_models_cache.clear()
    yield
    s._compat_models_cache.clear()


def _groups(client, admin_headers, monkeypatch, status=None, exc=None):
    monkeypatch.setattr(s, "offered_providers", lambda: {"gemini", "xai"})
    side = exc if exc is not None else (lambda *a, **k: _resp(status))
    with patch("app.routers.settings.requests.get", side_effect=side):
        r = client.get("/api/models", headers=admin_headers)
    assert r.status_code == 200
    return {g["provider"]: g["models"] for g in r.json()["groups"]}


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_refused_list_leaves_the_provider_out_of_the_picker(
        client, admin_headers, monkeypatch, status):
    groups = _groups(client, admin_headers, monkeypatch, status=status)
    assert "gemini" not in groups and "xai" not in groups
    assert s.compat_list_refused("gemini") and s.compat_list_refused("xai")


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_an_outage_or_a_rate_limit_still_serves_the_fallback(
        client, admin_headers, monkeypatch, status):
    groups = _groups(client, admin_headers, monkeypatch, status=status)
    assert groups["gemini"] == s._COMPAT_FALLBACK_MODELS["gemini"]
    assert groups["xai"] == s._COMPAT_FALLBACK_MODELS["xai"]


def test_no_network_still_serves_the_fallback(client, admin_headers, monkeypatch):
    groups = _groups(client, admin_headers, monkeypatch,
                     exc=requests.ConnectionError("offline"))
    assert groups["xai"] == s._COMPAT_FALLBACK_MODELS["xai"]


def test_a_refused_read_is_asked_again_after_five_minutes_not_an_hour():
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return _resp(401)
    with patch("app.routers.settings.requests.get", side_effect=fake_get):
        assert s._fetch_compat_models("gemini") == []
        s._fetch_compat_models("gemini")
        assert len(calls) == 1                       # cached inside the window
        s._compat_models_cache["gemini"]["ts"] -= s._REFUSED_CACHE_SECONDS + 1
        s._fetch_compat_models("gemini")
    assert len(calls) == 2


def test_saving_a_key_drops_the_old_keys_cached_list(client, admin_headers):
    from app.config import set_config
    s._compat_models_cache["gemini"] = {"ts": time.time(), "models": [], "all": [],
                                        "refused": True, "ttl": s._REFUSED_CACHE_SECONDS}
    try:
        r = client.put("/api/settings", headers=admin_headers,
                       json={"gemini_api_key": "gem-key-new", "current_password": "AdminPass1"})
        assert r.status_code == 200
        assert "gemini" not in s._compat_models_cache
    finally:
        set_config("gemini_api_key", "")  # don't leak state into other tests


def test_gemini_agents_and_its_image_family_leave_the_chat_picker():
    ids = ["gemini-flash-latest", "gemini-robotics-er-2-preview",
           "gemini-2.5-computer-use-preview-10-2025",
           "gemini-robotics-er-2-streaming-preview", "gemini-nano-banana-2.1",
           "gemini-pro-latest"]
    listed = [{"value": i, "label": i, "badge": "Gemini"} for i in ids]
    kept = [m["value"] for m in model_catalog.trim_to_newest(listed)]
    assert kept == ["gemini-flash-latest", "gemini-pro-latest"]
