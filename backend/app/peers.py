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


def resolve_peer_url(url: str, require_resolution: bool = True) -> tuple[str, str | None]:
    """Return (normalized peer URL, the ONE public address to connect to), or
    raise PeerURLRefused.

    Resolution happens here rather than trusting the hostname string: a name
    that looks external can resolve straight back to 169.254.169.254. Every
    address the name resolves to must pass, and the address returned is the
    one the caller MUST connect to (_pinned_request) - never the name again.
    Until 2026-09-13 this check handed the NAME back to requests, which
    resolved it a second time: a DNS-rebinding window in which a hostile
    resolver answers "public" to the check and the metadata address to the
    connect (hardening item 6, the rider on audit-tail entry 5). The address
    chosen is the first IPv4 (this host's egress is IPv4), else the first
    IPv6.

    require_resolution splits the two callers. At CONFIG time a name that does
    not resolve is refused, so the operator finds out at the panel instead of
    through silent per-chat failures. On the FETCH path it is not: a host with
    no address cannot reach anything, so there is nothing to protect against,
    and treating it as a security refusal would relabel every ordinary DNS
    outage - and every peer that is simply down - as an attack. Those belong to
    the circuit breaker, which is why the address comes back as None there and
    the caller records an ordinary failure without touching the network.
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
    if parsed.username or parsed.password:
        raise PeerURLRefused("Peer URL must not carry credentials")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        if require_resolution:
            raise PeerURLRefused(f"Peer host '{host}' does not resolve: {e}")
        return raw, None
    ips: list[str] = []
    for info in infos:
        ip_str = info[4][0]
        if ip_str not in ips:
            ips.append(ip_str)
    if not ips:
        if require_resolution:
            raise PeerURLRefused(f"Peer host '{host}' resolves to no address")
        return raw, None
    for ip_str in ips:
        reason = _refuse_addr(ipaddress.ip_address(ip_str))
        if reason:
            raise PeerURLRefused(f"Peer host '{host}' resolves to a {reason} ({ip_str}) - refused")
    for ip_str in ips:
        if ":" not in ip_str:
            return raw, ip_str
    return raw, ips[0]


def validate_peer_url(url: str, require_resolution: bool = True) -> str:
    """The config-time entry point: the normalized URL, or PeerURLRefused.
    The fetch paths use resolve_peer_url and connect to the address it
    returns; this wrapper exists for the panel and for callers that only
    need the verdict."""
    return resolve_peer_url(url, require_resolution)[0]


# -- Pinned connection (the guard's verdict is the address we connect to) -----

class _PinnedHostAdapter(_req.adapters.HTTPAdapter):
    """Send to the IP literal in the URL while presenting and verifying the
    REAL hostname: the Host header names it, and for https the same name is
    the TLS server name (SNI) and the name the certificate must match. An
    https URL with a bare IP would otherwise send no SNI and be handed a
    default certificate (or refused) - which is why "fetch the IP" is not by
    itself a safe pinning. urllib3 keys its pools on both settings, so a
    fresh Session per request never mixes hostnames. Ported from Kin's
    webfetch (`_PinnedHostAdapter`, 2026-09-11) to the peer lane 2026-09-13."""

    def __init__(self, hostname: str, **kwargs):
        self._hostname = hostname
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        pool_kw = self.poolmanager.connection_pool_kw
        if (request.url or "").lower().startswith("https://"):
            pool_kw["server_hostname"] = self._hostname
            pool_kw["assert_hostname"] = self._hostname
        else:
            pool_kw.pop("server_hostname", None)
            pool_kw.pop("assert_hostname", None)
        return super().send(request, **kwargs)


def _pinned_request(method: str, url: str, ip: str | None, *, timeout: float,
                    headers: dict | None = None, params: dict | None = None):
    """Send `method url` over a connection to `ip` - the address
    resolve_peer_url validated for its hostname - with no second DNS lookup
    anywhere: the URL sent down the socket carries the IP literal, the Host
    header (and, for https, the TLS server name via _PinnedHostAdapter)
    carries the hostname, so the peer sees the request it expects and its
    certificate is still verified against the real name. Redirects are never
    followed: a hop to a NAME would be a second, unpinned resolution. Refuses
    to run without an address - a request this module did not pin is a
    request this module does not make. The caller closes the response with
    _release (the Session behind it owns the pooled connection)."""
    if not ip:
        raise PeerURLRefused("refusing an unpinned peer request (no validated address)")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    literal = f"[{ip}]" if ":" in ip else ip
    netloc = f"{literal}:{port}" if port else literal
    pinned = parsed._replace(netloc=netloc).geturl()
    hdrs = dict(headers or {})
    hdrs["Host"] = f"{host}:{port}" if port else host
    session = _req.Session()
    adapter = _PinnedHostAdapter(host)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    try:
        resp = session.request(method, pinned, headers=hdrs, params=params,
                               timeout=timeout, allow_redirects=False)
    except Exception:
        session.close()
        raise
    setattr(resp, "_pinned_session", session)
    return resp


def _release(resp) -> None:
    """Close a response from _pinned_request and the session behind it."""
    try:
        resp.close()
    except Exception:
        pass
    session = getattr(resp, "_pinned_session", None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


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
        # saved, both arrive here. The address it returns is the one we connect
        # to (pinned) - the name is never resolved a second time.
        safe, ip = resolve_peer_url(url, require_resolution=False)
        if ip is None:
            return False
        r = _pinned_request("GET", f"{safe}/api/health", ip, timeout=timeout)
        try:
            return r.status_code == 200
        finally:
            _release(r)
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
        url, ip = resolve_peer_url(url, require_resolution=False)
    except PeerURLRefused as e:
        # Counts as a peer failure so the breaker opens on a row that will
        # never be fetchable, instead of re-resolving it on every chat.
        log.warning("peer '%s' refused: %s", name, e)
        _record_failure(peer_id, str(e))
        return []
    if ip is None:
        # An ordinary outage, not a security refusal: nothing to connect to,
        # so nothing is connected to - the breaker learns it like any other.
        _record_failure(peer_id, "host does not resolve")
        log.warning("Peer '%s' host does not resolve", name)
        return []

    headers = {"X-Peer-Key": _PEER_API_KEY} if _PEER_API_KEY else {}
    log.info("Querying peer '%s' at %s/api/query-kb (q=%r, n=%d)", name, url, query[:80], n_results)
    t0 = time.monotonic()
    resp = None
    try:
        resp = _pinned_request("GET", f"{url}/api/query-kb", ip,
                               params={"q": query, "n": n_results},
                               headers=headers, timeout=timeout)
        if 300 <= resp.status_code < 400:
            # Never followed: a redirect to a NAME would be a second, unpinned
            # resolution. The operator registers the final URL instead.
            raise _req.exceptions.HTTPError(
                f"peer redirected ({resp.status_code}) - register the final URL",
                response=resp)
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
    finally:
        if resp is not None:
            _release(resp)
