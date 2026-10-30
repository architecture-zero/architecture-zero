"""The Ollama connection test fails on an answer that is not a model list
(2026-10-30).

GET /api/settings/test-ollama read any JSON answer as Ollama's tag list: it
never looked at the status, and `.get("models", [])` turned a body with no
list into zero models. So a wrong base URL that answers JSON - another
service's 404 page, a health route - showed "Connected - 0 model(s)", and the
owner saved a setting that answers no chat.

FIX: a 404 with a JSON body, and a 200 whose body carries no model list, each
answer ok false with the route's fixed sentence; the log names what came back,
bounded at 300. CONTROL: a real model list still answers connected with its
count, an empty one included (an Ollama with nothing pulled yet is reachable).
"""
import pytest

from app.routers import settings as settings_router

FIXED = "The Ollama connection test failed. See the server log."


class _Answer:
    """What requests.get returns, as far as the route reads it."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _answer_with(monkeypatch, status_code: int, body) -> list:
    monkeypatch.setattr(settings_router, "_ollama_get",
                        lambda path, timeout=5: _Answer(status_code, body))
    seen: list = []
    monkeypatch.setattr(settings_router, "log_error",
                        lambda event, **kw: seen.append((event, kw)), raising=False)
    return seen


@pytest.mark.parametrize("status_code, body", [
    (404, {"detail": "Not Found"}),
    (200, {"status": "ok", "service": "something else"}),
], ids=["404-with-json", "200-without-a-model-list"])
def test_an_answer_that_is_not_a_model_list_fails(
        monkeypatch, client, admin_headers, status_code, body):
    """FIX: neither answer reads as connected; the log says what came back."""
    seen = _answer_with(monkeypatch, status_code, body)
    r = client.get("/api/settings/test-ollama", headers=admin_headers)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["ok"] is False, answer
    assert answer["error"] == FIXED
    assert "model_count" not in answer
    [(_, fields)] = [e for e in seen if e[0] == "ollama_connection_test_failed"]
    assert str(status_code) in fields["error"]
    assert len(fields["error"]) <= 300


@pytest.mark.parametrize("models", [
    [{"name": "a"}, {"name": "b"}, {"name": "c"}],
    [],
], ids=["three-models", "nothing-pulled-yet"])
def test_a_real_model_list_still_answers_connected(
        monkeypatch, client, admin_headers, models):
    """CONTROL: Ollama's own answer is connected, with its count."""
    seen = _answer_with(monkeypatch, 200, {"models": models})
    r = client.get("/api/settings/test-ollama", headers=admin_headers)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["ok"] is True and answer["model_count"] == len(models)
    assert "error" not in answer
    assert not seen
