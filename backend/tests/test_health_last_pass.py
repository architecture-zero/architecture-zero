"""The health routes read the self-check's last pass for Ollama and Redis
(2026-10-07, the AZ-02 security read's Info).

/api/health and /api/health/ready are unauthenticated, and every hit read
Ollama's model list and pinged Redis - an outbound call per anonymous request,
across the network wherever Ollama runs on another host. Now each self-check
pass records what it found and the routes read that, as readiness reads the
retrieval lane.

The never-calls tests fail on the code before the change (each hit reached
requests.get, and readiness reached redis.from_url); the rest pin the words.
This file is the same on every surface.
"""
import time

import pytest
import redis
import requests

from app import self_check as sc

_BOUND = 2 * 300 + 60   # two intervals and a minute, at the 300 s interval


class _Tags:
    status_code = 200


class _Redis:
    def __init__(self, up=True):
        self._up = up

    def ping(self):
        if not self._up:
            raise redis.ConnectionError("refused")
        return True


def _ollama_down(path, timeout):
    raise requests.ConnectionError("refused")


@pytest.fixture
def watched(monkeypatch):
    """The timer started at the 300 s interval, watching Ollama and Redis, no
    pass yet; alerts recorded instead of sent."""
    monkeypatch.setenv("ENABLE_OLLAMA", "true")
    monkeypatch.setenv("REDIS_URL", "redis://redis.test:6379/0")
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 300)
    monkeypatch.setattr(sc, "_started_at", time.time(), raising=False)
    monkeypatch.setattr(sc, "_ollama_wired", True, raising=False)
    monkeypatch.setattr(sc, "_redis_wired", True, raising=False)
    monkeypatch.setattr(sc, "_ollama_last", None, raising=False)
    monkeypatch.setattr(sc, "_redis_last", None, raising=False)
    monkeypatch.setattr(sc, "_last_failing", ())
    try:
        import app.boot_history as _boot_history
        monkeypatch.setattr(_boot_history, "crash_loop_state", lambda: {"looping": False})
    except ImportError:
        pass
    fired = []
    monkeypatch.setattr(sc, "fire_alert", lambda key, *a, **k: fired.append(key))
    return fired


@pytest.fixture
def tripwire(monkeypatch):
    """Every call a health request makes out to Ollama or Redis."""
    touched = []

    def _get(*a, **k):
        touched.append(("ollama", a[0] if a else k.get("url")))
        return _Tags()

    def _from_url(*a, **k):
        touched.append(("redis", a[0] if a else k.get("url")))
        return _Redis()
    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(redis, "from_url", _from_url)
    return touched


def _words(client):
    checks = client.get("/api/health/ready").json()["checks"]
    return checks.get("ollama"), checks.get("redis")


def test_readiness_never_calls_ollama_or_redis(client, watched, tripwire):
    for _ in range(3):
        client.get("/api/health/ready")
    assert tripwire == []


def test_health_never_calls_ollama(client, watched, tripwire):
    for _ in range(3):
        assert client.get("/api/health").status_code == 200
    assert tripwire == []


def test_the_words_come_from_the_last_pass(client, watched, monkeypatch, tmp_path):
    assert _words(client) == ("pending", "pending")
    assert client.get("/api/health").json() == {"status": "healthy", "ollama": "pending"}

    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Redis(up=False))
    sc.run_self_check(str(tmp_path), ollama_get=_ollama_down)
    assert _words(client) == ("unreachable", "error")
    assert client.get("/api/health").json() == {"status": "degraded", "ollama": "unreachable"}
    assert "ollama_down" in watched

    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Redis(up=True))
    sc.run_self_check(str(tmp_path), ollama_get=lambda path, timeout: _Tags())
    assert _words(client) == ("ok", "ok")
    assert client.get("/api/health").json() == {"status": "healthy", "ollama": "connected"}


def test_neither_fails_readiness(client, watched, monkeypatch, tmp_path):
    """Ollama is one provider among several; the backend runs without Redis."""
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Redis(up=False))
    sc.run_self_check(str(tmp_path), ollama_get=_ollama_down)
    r = client.get("/api/health/ready")
    assert r.json()["checks"]["ollama"] == "unreachable"
    assert r.json()["checks"]["redis"] == "error"
    assert r.status_code == 200, r.json()


def test_a_stale_pass_says_stale(client, watched, monkeypatch):
    long_ago = time.time() - _BOUND - 5
    monkeypatch.setattr(sc, "_ollama_last", {"ok": True, "at": long_ago})
    monkeypatch.setattr(sc, "_redis_last", {"ok": True, "at": long_ago})
    assert _words(client) == ("stale", "stale")


def test_unwatched_reads_skipped_and_the_timer_off_reads_off(client, watched, monkeypatch):
    monkeypatch.setattr(sc, "_ollama_wired", False)
    monkeypatch.setattr(sc, "_redis_wired", False)
    assert _words(client) == ("skipped", "skipped")
    assert client.get("/api/health").json() == {"status": "healthy", "ollama": "skipped"}
    monkeypatch.setattr(sc, "_ollama_wired", True)
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 0)
    assert _words(client)[0] == "off"


def test_a_pass_with_no_redis_configured_does_not_ping(watched, monkeypatch, tmp_path, tripwire):
    monkeypatch.delenv("REDIS_URL")
    result = sc.run_self_check(str(tmp_path))
    assert "redis" not in result and tripwire == []
