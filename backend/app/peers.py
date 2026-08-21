"""Eco Mode peer queries - with per-peer health tracking + a circuit breaker.

A hub fans out to EVERY enabled peer on each eco-enabled chat, so a single
down peer adds its full timeout to every chat - once per down peer in the
worst case. The breaker skips a peer after _CB_THRESHOLD consecutive
failures and retries it after _CB_BACKOFF_SECONDS - failures degrade to
"that peer contributes nothing", never to a hung chat.
"""
import ipaddress
import json
import logging
import os
import socket
import time
from urllib.parse import urlparse
import requests as _req
from app.config import get_config, set_config

log = logging.getLogger(__name__)

_PEER_API_KEY       = os.getenv("PEER_API_KEY", "")
_CB_THRESHOLD       = int(os.getenv("PEER_CIRCUIT_BREAKER_THRESHOLD", "3"))
_CB_BACKOFF_SECONDS = int(os.getenv("PEER_CIRCUIT_BREAKER_BACKOFF", "300"))

# A peer URL is an operator-supplied address the SERVER then fetches - the
# textbook SSRF shape. Without this the box is a proxy into anything it can
# reach: the cloud metadata service (169.254.169.254 hands out IAM
# credentials), loopback admin ports, the private subnets it sits inside.
# Owner-only configuration is not the control it looks like - it bounds who
# can aim the request, not what the request can reach, and an Owner session is
# exactly what an attacker who got that far already has.
#
# Escape hatch for the legitimate case: peers on a private LAN are a real
# deployment, so PEER_ALLOW_PRIVATE=true re-permits private ranges. Loopback
# and link-local stay refused either way - neither is ever a peer, and
# link-local is the metadata address.
_ALLOW_PRIVATE = os.getenv("PEER_ALLOW_PRIVATE", "false").lower() == "true"


class PeerURLRefused(ValueError):
    """A configured peer URL failed the SSRF guard."""


def _refuse_addr(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        # 169.254.0.0/16 - the cloud metadata service lives here.
        return "link-local address"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return "reserved address"
    if ip.is_private and not _ALLOW_PRIVATE:
        return "private address (set PEER_ALLOW_PRIVATE=true for LAN peers)"
    return None


def validate_peer_url(url: str, require_resolution: bool = True) -> str:
    """Return the normalized peer URL, or raise PeerURLRefused.

    Resolution happens here rather than trusting the hostname string: a name
    that looks external can resolve straight back to 169.254.169.254. Every
    address the name resolves to must pass, since which one requests picks is
    not ours to choose. A config-time check alone cannot close the DNS-rebinding
    window, which is why this also runs on the fetch path.

    require_resolution splits the two callers. At CONFIG time a name that does
    not resolve is refused, so the operator finds out at the panel instead of
    through silent per-chat failures. On the FETCH path it is not: a host with
    no address cannot reach anything, so there is nothing to protect against,
    and treating it as a security refusal would relabel every ordinary DNS
    outage - and every peer that is simply down - as an attack. Those belong to
    the circuit breaker, with the connection error that actually describes them.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise PeerURLRefused("Peer URL is empty")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise PeerURLRefused(f"Peer URL scheme must be http or https, got '{parsed.scheme or raw}'")
    host = parsed.hostname
    if not host:
        raise PeerURLRefused("Peer URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        if require_resolution:
            raise PeerURLRefused(f"Peer host '{host}' does not resolve: {e}")
        return raw
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        reason = _refuse_addr(ip)
        if reason:
            raise PeerURLRefused(f"Peer host '{host}' resolves to a {reason} ({ip}) - refused")
    return raw


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
        # Re-validated on the fetch path, not only at config time: a stored row
        # predating the guard, or a name that re-resolves inward after it was
        # saved, both arrive here.
        safe = validate_peer_url(url, require_resolution=False)
        r = _req.get(f"{safe}/api/health", timeout=timeout)
        return r.status_code == 200
    except PeerURLRefused as e:
        log.warning("peer health check refused: %s", e)
        return False
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

    try:
        url = validate_peer_url(url, require_resolution=False)
    except PeerURLRefused as e:
        # Counts as a peer failure so the breaker opens on a row that will
        # never be fetchable, instead of re-resolving it on every chat.
        log.warning("peer '%s' refused: %s", name, e)
        _record_failure(peer_id, str(e))
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
