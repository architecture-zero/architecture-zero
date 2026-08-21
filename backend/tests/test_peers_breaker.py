"""Peer circuit breaker.

The through-line: a down peer must degrade to "contributes nothing" instantly,
never to a per-chat timeout - and with several peers on the fan-out, never to
stacked timeouts. Failures open the circuit after the threshold; an open
circuit skips the network entirely; success (or the backoff window, or a
manual reset) closes it.
"""
import time

import requests as _requests

import app.peers as peers
from app.peers import (
    query_peer_kb,
    get_peer_health,
    get_peers_with_health,
    reset_peer_circuit_breaker,
    _CB_THRESHOLD,
)

_PEER = {"id": "test-peer", "name": "Test Peer", "url": "http://peer.invalid:9"}


def _fail_get(*a, **k):
    raise _requests.exceptions.ConnectionError("refused")


def _reset():
    peers._save_peer_health(_PEER["id"], {})


def test_breaker_opens_after_threshold_and_skips_the_network(monkeypatch):
    _reset()
    calls = {"n": 0}

    def counting_fail(*a, **k):
        calls["n"] += 1
        raise _requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(peers._req, "get", counting_fail)
    for _ in range(_CB_THRESHOLD):
        assert query_peer_kb(_PEER, "q") == []
    h = get_peer_health(_PEER["id"])
    assert h["circuit_open"] is True
    assert h["consecutive_failures"] == _CB_THRESHOLD
    assert calls["n"] == _CB_THRESHOLD

    # Open circuit: the next query returns [] WITHOUT touching the network.
    assert query_peer_kb(_PEER, "q") == []
    assert calls["n"] == _CB_THRESHOLD


def test_backoff_expiry_retries_and_success_closes(monkeypatch):
    _reset()
    monkeypatch.setattr(peers._req, "get", _fail_get)
    for _ in range(_CB_THRESHOLD):
        query_peer_kb(_PEER, "q")
    assert get_peer_health(_PEER["id"])["circuit_open"] is True

    # Age the last failure past the backoff window - the breaker half-opens.
    h = get_peer_health(_PEER["id"])
    h["last_failure_at"] = time.time() - peers._CB_BACKOFF_SECONDS - 1
    peers._save_peer_health(_PEER["id"], h)

    class _OK:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"results": [{"text": "t", "source": "s.md"}]}

    monkeypatch.setattr(peers._req, "get", lambda *a, **k: _OK())
    out = query_peer_kb(_PEER, "q")
    assert len(out) == 1 and out[0]["peer"] == "Test Peer"
    h = get_peer_health(_PEER["id"])
    assert h["circuit_open"] is False and h["consecutive_failures"] == 0
    assert h["last_latency_ms"] is not None


def test_manual_reset_closes_an_open_circuit(monkeypatch):
    _reset()
    monkeypatch.setattr(peers._req, "get", _fail_get)
    for _ in range(_CB_THRESHOLD):
        query_peer_kb(_PEER, "q")
    assert get_peer_health(_PEER["id"])["circuit_open"] is True
    reset_peer_circuit_breaker(_PEER["id"])
    assert get_peer_health(_PEER["id"])["circuit_open"] is False


def test_health_rides_the_peer_listing(monkeypatch):
    _reset()
    monkeypatch.setattr(peers, "get_peers", lambda: [dict(_PEER, enabled=True)])
    monkeypatch.setattr(peers._req, "get", _fail_get)
    query_peer_kb(_PEER, "q")
    listed = get_peers_with_health()
    assert listed[0]["consecutive_failures"] == 1
    assert listed[0]["total_errors"] == 1
    assert "connection error" in (listed[0]["last_error"] or "")
