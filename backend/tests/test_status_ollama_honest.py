"""/api/status says Ollama is connected only on Ollama's own answer
(2026-10-30).

status() set ollama_ok the moment /api/tags answered ANY JSON: the status
was never read, and the model list was read after the flag was already set.
So a wrong base URL that answers JSON - another service's 404 page, a health
route, a bare list - reported "connected" with no models. GET
/api/settings/test-ollama was cured of the same read the same day; this is
its twin on the status surface.

FIX: a 404 with a JSON body, a 200 whose body carries no model list, and a
200 whose body is not an object each report "unreachable". CONTROL: a real
model list reports connected with its names, an empty one included (an
Ollama with nothing pulled yet is reachable). And the three routes that list
Ollama's models read the base URL saved in Settings, not the environment.
"""
import pytest

import app.config as app_config
from app.routers import system as system_router


class _Answer:
    """What requests.get returns, as far as the route reads it."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _status_with(monkeypatch, client, admin_headers, status_code: int, body) -> dict:
    monkeypatch.setattr(system_router, "_ollama_get",
                        lambda path, timeout=5: _Answer(status_code, body))
    r = client.get("/api/status", headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("status_code, body", [
    (404, {"detail": "Not Found"}),
    (200, {"status": "ok", "service": "something else"}),
    (200, ["not", "an", "object"]),
], ids=["404-with-json", "200-without-a-model-list", "200-with-a-bare-list"])
def test_an_answer_that_is_not_a_model_list_reads_unreachable(
        monkeypatch, client, admin_headers, status_code, body):
    """FIX: none of these is Ollama answering."""
    answer = _status_with(monkeypatch, client, admin_headers, status_code, body)
    assert answer["ollama"] == "unreachable", answer["ollama"]
    assert answer["models_available"] == []


@pytest.mark.parametrize("models", [
    [{"name": "a"}, {"name": "b"}],
    [],
], ids=["two-models", "nothing-pulled-yet"])
def test_a_real_model_list_still_reads_connected(
        monkeypatch, client, admin_headers, models):
    """CONTROL: Ollama's own answer is connected, with its names."""
    answer = _status_with(monkeypatch, client, admin_headers, 200, {"models": models})
    assert answer["ollama"] == "connected"
    assert answer["models_available"] == [m["name"] for m in models]


_SAVED = "http://saved-in-settings.invalid:11434"


@pytest.mark.parametrize("path", ["/api/status", "/api/admin/models", "/api/models"])
def test_every_model_list_reads_the_base_url_saved_in_settings(
        monkeypatch, client, admin_headers, path):
    """CONTROL: the base URL saved in Settings is the one asked - the read
    _ollama_get gives every caller - so after an operator changes it, the
    picker, the admin list and the status line all ask the new server."""
    real = app_config.get_config
    monkeypatch.setattr(app_config, "get_config",
                        lambda key, default="": _SAVED if key == "ollama_base_url"
                        else real(key, default))
    monkeypatch.setattr("app.routers.settings.offered_providers", lambda: {"ollama"})
    asked: list = []

    def _get(url, headers=None, timeout=None, **kw):
        asked.append(url)
        return _Answer(200, {"models": [{"name": "m"}]})

    monkeypatch.setattr("app.runtime_config.requests.get", _get)
    r = client.get(path, headers=admin_headers)
    assert r.status_code == 200, r.text
    tags = [u for u in asked if u.endswith("/api/tags")]
    assert tags and all(u == f"{_SAVED}/api/tags" for u in tags), asked
