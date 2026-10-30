"""The Ollama connection test answers a fixed sentence (2026-10-30).

GET /api/settings/test-ollama put str(e) in its answer on any failure, so the
Settings tab showed the HTTP library's own words: the host and port it tried,
its retry state, its internal object repr - and whatever else an exception
carries, unbounded. The rule for every surface is a fixed sentence to the
caller and the words to the log, bounded at 300 characters.

FIX: a connection test whose request raises answers ok false with exactly
the fixed sentence and none of the library's words; the log gets them, cut
to 300. CONTROL: a working Ollama answers as before and logs no error.
"""
from app.routers import settings as settings_router

FIXED = "The Ollama connection test failed. See the server log."
# The library's own words: an internal host, its retry state, an object
# repr, and far more than the log keeps.
LIBRARY_WORDS = ("HTTPConnectionPool(host='10.0.0.7', port=11434): Max retries "
                 "exceeded with url: /api/tags (Caused by NewConnectionError("
                 "'<urllib3.connection.HTTPConnection object at 0x7f3a>: Failed "
                 "to establish a new connection: [Errno 111] Connection refused')) "
                 + "x" * 2000)


class _Tags:
    def json(self):
        return {"models": [{"name": "a"}, {"name": "b"}]}


def _record_log_errors(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(settings_router, "log_error",
                        lambda event, **kw: seen.append((event, kw)), raising=False)
    return seen


def test_a_failed_connection_test_answers_the_fixed_sentence(
        monkeypatch, client, admin_headers):
    """FIX: the owner's tab gets the fixed sentence; the log gets the words."""
    def refuse(path, timeout=5):
        raise ConnectionError(LIBRARY_WORDS)

    monkeypatch.setattr(settings_router, "_ollama_get", refuse)
    seen = _record_log_errors(monkeypatch)
    r = client.get("/api/settings/test-ollama", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == FIXED
    assert "10.0.0.7" not in r.text and "Max retries" not in r.text
    [(_, fields)] = [e for e in seen if e[0] == "ollama_connection_test_failed"]
    assert fields["error"] == f"ConnectionError: {LIBRARY_WORDS}"[:300], len(fields["error"])


def test_a_working_connection_test_answers_as_before(monkeypatch, client, admin_headers):
    """CONTROL: Ollama answers, the model count comes back, nothing is logged."""
    monkeypatch.setattr(settings_router, "_ollama_get", lambda path, timeout=5: _Tags())
    seen = _record_log_errors(monkeypatch)
    r = client.get("/api/settings/test-ollama", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["model_count"] == 2
    assert "error" not in body
    assert not seen
