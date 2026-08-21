"""Eco Mode peer queries - with per-peer health tracking + a circuit breaker.

A hub fans out to EVERY enabled peer on each eco-enabled chat, so a single
down peer adds its full timeout to every chat - once per down peer in the
worst case. The breaker skips a peer after _CB_THRESHOLD consecutive
failures and retries it after _CB_BACKOFF_SECONDS - failures degrade to
"that peer contributes nothing", never to a hung chat.
"""
import json
import logging
import os
import time
import requests as _req
from app.config import get_config, set_config

log = logging.getLogger(__name__)

_PEER_API_KEY       = os.getenv("PEER_API_KEY", "")
_CB_THRESHOLD       = int(os.getenv("PEER_CIRCUIT_BREAKER_THRESHOLD", "3"))
_CB_BACKOFF_SECONDS = int(os.getenv("PEER_CIRCUIT_BREAKER_BACKOFF", "300"))


# -- Peer list ----------------------------------------------------------------

def get_peers() -> list[dict]:
    raw = get_config("ai_peers", "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_peers(peers: list[dict]):
    set_config("ai_peers", json.dumps(peers))


# -- Health tracking ----------------------------------------------------------

def _health_key(peer_id: str) -> str:
    return f"peer_health:{peer_id}"


def get_peer_health(peer_id: str) -> dict:
    raw = get_config(_health_key(peer_id), "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_peer_health(peer_id: str, data: dict):
    set_config(_health_key(peer_id), json.dumps(data))


def reset_peer_circuit_breaker(peer_id: str):
    h = get_peer_health(peer_id)
    h["consecutive_failures"] = 0
    h["circuit_open"] = False
    _save_peer_health(peer_id, h)


def _record_success(peer_id: str, latency_ms: int, chunk_count: int):
    h = get_peer_health(peer_id)
    h["last_seen"]            = time.time()
    h["last_latency_ms"]      = latency_ms
    h["consecutive_failures"] = 0
    h["circuit_open"]         = False
    h["total_queries"]        = h.get("total_queries", 0) + 1
    h["total_chunks"]         = h.get("total_chunks", 0) + chunk_count
    _save_peer_health(peer_id, h)


def _record_failure(peer_id: str, error: str):
    h = get_peer_health(peer_id)
    failures = h.get("consecutive_failures", 0) + 1
    h["consecutive_failures"] = failures
    h["last_failure_at"]      = time.time()
    h["last_error"]           = error
    h["total_queries"]        = h.get("total_queries", 0) + 1
    h["total_errors"]         = h.get("total_errors", 0) + 1
    if failures >= _CB_THRESHOLD:
        h["circuit_open"] = True
        log.warning("circuit-break %s - %d consecutive failures, skipping for %ds",
                    peer_id, failures, _CB_BACKOFF_SECONDS)
    _save_peer_health(peer_id, h)


def _circuit_open(peer_id: str) -> bool:
    h = get_peer_health(peer_id)
    if not h.get("circuit_open"):
        return False
    last_failure = h.get("last_failure_at", 0)
    if time.time() - last_failure > _CB_BACKOFF_SECONDS:
        log.info("circuit-break backoff expired for %s - retrying", peer_id)
        return False
    return True


# -- Health check -------------------------------------------------------------

def check_peer_health(url: str, timeout: int = 5) -> bool:
    try:
        r = _req.get(f"{url.rstrip('/')}/api/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def get_peers_with_health() -> list[dict]:
    peers = get_peers()
    result = []
    for p in peers:
        h = get_peer_health(p.get("id") or p.get("name", "?"))
        merged = dict(p)
        merged["last_seen"]            = h.get("last_seen")
        merged["last_latency_ms"]      = h.get("last_latency_ms")
        merged["consecutive_failures"] = h.get("consecutive_failures", 0)
        merged["circuit_open"]         = h.get("circuit_open", False)
        merged["total_queries"]        = h.get("total_queries", 0)
        merged["total_errors"]         = h.get("total_errors", 0)
        merged["last_error"]           = h.get("last_error")
        result.append(merged)
    return result


# -- Query --------------------------------------------------------------------

def query_peer_kb(peer: dict, query: str, n_results: int = 8, timeout: int = 10) -> list[dict]:
    """Query a remote peer's knowledge base directly - returns raw chunks, no
    AI call. A peer with an open circuit is skipped instantly (no network
    call, no timeout)."""
    peer_id = peer.get("id") or peer.get("name", peer.get("url", "?"))
    url     = peer["url"].rstrip("/")
    name    = peer.get("name", url)

    if _circuit_open(peer_id):
        log.info("skipping peer '%s' (circuit open)", name)
        return []

    headers = {"X-Peer-Key": _PEER_API_KEY} if _PEER_API_KEY else {}
    log.info("Querying peer '%s' at %s/api/query-kb (q=%r, n=%d)", name, url, query[:80], n_results)
    t0 = time.monotonic()
    try:
        resp = _req.get(
            f"{url}/api/query-kb",
            params={"q": query, "n": n_results},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        chunks = resp.json().get("results", [])
        latency_ms = int((time.monotonic() - t0) * 1000)
        _record_success(peer_id, latency_ms, len(chunks))
        log.info("Peer '%s' returned %d chunks in %dms", name, len(chunks), latency_ms)
        for chunk in chunks:
            chunk["peer"] = name
        return chunks
    except _req.exceptions.Timeout:
        _record_failure(peer_id, f"timeout after {timeout}s")
        log.warning("Peer '%s' timed out after %ds", name, timeout)
        return []
    except _req.exceptions.ConnectionError as exc:
        _record_failure(peer_id, f"connection error: {exc}")
        log.warning("Peer '%s' connection error: %s", name, exc)
        return []
    except _req.exceptions.HTTPError as exc:
        _record_failure(peer_id, f"HTTP {exc.response.status_code}")
        log.warning("Peer '%s' HTTP %s: %s", name, exc.response.status_code, exc.response.text[:200])
        return []
    except Exception as exc:
        _record_failure(peer_id, repr(exc))
        log.exception("Peer '%s' unexpected error: %s", name, exc)
        return []
