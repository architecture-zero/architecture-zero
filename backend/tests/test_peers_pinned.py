"""The peer lane's SSRF guard closes its resolve-then-connect window by PINNING.

Hardening item 6, the rider on audit-tail entry 5 (2026-09-13): the guard
resolved the peer's hostname for the check and then handed the NAME to
requests, which resolved it again - a DNS-rebinding window in which a hostile
resolver answers "public" to the check and 169.254.169.254 to the connect.
The URLs here are operator-registered peers, so the rebinding attacker is a
registered peer's own operator: the same class as the webfetch finding, lower
exposure, closed the same way. These tests pin the closure: the address the
guard validated is the address the socket goes to, the hostname rides in the
Host header and the TLS server name, and nothing in the peer path ever asks
DNS a second time. The HTTP adapter's real `send` is replaced one level up
(`requests.adapters.HTTPAdapter.send`) so `_PinnedHostAdapter`'s own logic
still runs and the request it built can be inspected.
"""
import io
import json

import pytest
import requests
import requests.adapters

from app import peers

PUBLIC = "93.184.216.34"
METADATA = "169.254.169.254"
PUBLIC_V6 = "2606:4700:4700::1111"

PEER = {"id": "p1", "name": "Peer One", "url": "https://peer.example.com"}


@pytest.fixture(autouse=True)
def recorders(monkeypatch):
    """No breaker state and no config writes: the recorders are observed, not
    run, and the circuit is always closed."""
    monkeypatch.setattr(peers, "_circuit_open", lambda peer_id: False)
    successes: list = []
    failures: list = []
    monkeypatch.setattr(peers, "_record_success", lambda *a: successes.append(a))
    monkeypatch.setattr(peers, "_record_failure", lambda *a: failures.append(a))
    return successes, failures


def _resolver(monkeypatch, answers):
    """answers: {hostname: [ip, ...] | Exception} or a callable(host, call_index)
    -> [ips]. Records every lookup so a test can prove how often DNS was asked."""
    calls: list = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        ips = answers(host, len(calls)) if callable(answers) else answers[host]
        if isinstance(ips, Exception):
            raise ips
        return [(None, None, None, None, (ip, port)) for ip in ips]

    monkeypatch.setattr(peers.socket, "getaddrinfo", fake_getaddrinfo)
    return calls


def _response(status=200, payload=None, location=None):
    resp = requests.Response()
    resp.status_code = status
    resp.headers["content-type"] = "application/json"
    if location:
        resp.headers["location"] = location
    body = payload if payload is not None else {"results": [{"text": "t", "source": "s.md"}]}
    resp.raw = io.BytesIO(json.dumps(body).encode())
    return resp


def _capture_sends(monkeypatch, route=None):
    """Replace the base adapter send. `route(request) -> Response` picks the
    reply; default is a 200 with one chunk. Returns the list of
    (pool_kw_at_send, prepared_request) pairs."""
    sent: list = []

    def fake_send(self, request, **kwargs):
        sent.append((dict(self.poolmanager.connection_pool_kw), request))
        resp = route(request) if route else _response()
        resp.url = request.url
        resp.request = request
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    return sent


def test_query_connects_to_the_validated_address_not_the_name(monkeypatch, recorders):
    lookups = _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch)
    out = peers.query_peer_kb(PEER, "what is up", n_results=3)
    assert [c["peer"] for c in out] == ["Peer One"]
    assert len(sent) == 1
    pool_kw, req = sent[0]
    assert req.url.startswith(f"https://{PUBLIC}/api/query-kb?")   # the socket goes to the IP
    assert "q=what+is+up" in req.url and "n=3" in req.url
    assert req.headers["Host"] == "peer.example.com"                # the peer sees its name
    assert pool_kw["server_hostname"] == "peer.example.com"         # TLS SNI is the name
    assert pool_kw["assert_hostname"] == "peer.example.com"         # the cert must match it
    assert lookups == ["peer.example.com"]                          # DNS asked exactly once
    successes, failures = recorders
    assert len(successes) == 1 and failures == []


def test_a_rebinding_resolver_cannot_move_the_connection(monkeypatch, recorders):
    """First answer public, every later answer the metadata address - the
    classic rebinding. The query must land on the first answer and never
    ask again."""
    lookups = _resolver(monkeypatch, lambda host, i: [PUBLIC] if i == 1 else [METADATA])
    sent = _capture_sends(monkeypatch)
    peers.query_peer_kb(PEER, "q")
    assert lookups == ["peer.example.com"]
    assert len(sent) == 1
    assert sent[0][1].url.startswith(f"https://{PUBLIC}/")
    assert METADATA not in sent[0][1].url


def test_the_metadata_address_is_refused_before_any_connection(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [METADATA]})
    sent = _capture_sends(monkeypatch)
    assert peers.query_peer_kb(PEER, "q") == []
    assert sent == []
    successes, failures = recorders
    assert len(failures) == 1 and "link-local" in failures[0][1]


def test_a_mixed_answer_is_refused_before_any_connection(monkeypatch, recorders):
    """Which address a client would pick is not ours to choose, so all must pass."""
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC, "127.0.0.1"]})
    sent = _capture_sends(monkeypatch)
    assert peers.query_peer_kb(PEER, "q") == []
    assert sent == []


def test_an_unresolvable_peer_makes_no_connection_and_counts_as_an_outage(monkeypatch, recorders):
    """Not a security refusal: nothing to connect to, so nothing is connected
    to, and the breaker learns it like any other failure."""
    _resolver(monkeypatch, {"peer.example.com": peers.socket.gaierror("no such host")})
    sent = _capture_sends(monkeypatch)
    assert peers.query_peer_kb(PEER, "q") == []
    assert sent == []
    successes, failures = recorders
    assert failures == [("p1", "host does not resolve")]


def test_plain_http_sets_the_host_header_and_no_tls_name(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch)
    peers.query_peer_kb(dict(PEER, url="http://peer.example.com"), "q")
    pool_kw, req = sent[0]
    assert req.url.startswith(f"http://{PUBLIC}/api/query-kb")
    assert req.headers["Host"] == "peer.example.com"
    assert "server_hostname" not in pool_kw and "assert_hostname" not in pool_kw


def test_an_explicit_port_is_kept_in_the_url_and_the_host_header(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch)
    peers.query_peer_kb(dict(PEER, url="https://peer.example.com:8443"), "q")
    pool_kw, req = sent[0]
    assert req.url.startswith(f"https://{PUBLIC}:8443/api/query-kb")
    assert req.headers["Host"] == "peer.example.com:8443"
    assert pool_kw["server_hostname"] == "peer.example.com"


def test_ipv4_is_preferred_and_ipv6_is_bracketed(monkeypatch, recorders):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC_V6, PUBLIC]})
    sent = _capture_sends(monkeypatch)
    peers.query_peer_kb(PEER, "q")
    assert sent[0][1].url.startswith(f"https://{PUBLIC}/")
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC_V6]})
    sent = _capture_sends(monkeypatch)
    peers.query_peer_kb(PEER, "q")
    assert sent[0][1].url.startswith(f"https://[{PUBLIC_V6}]/")


def test_the_health_check_is_pinned_the_same_way(monkeypatch):
    lookups = _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(monkeypatch, route=lambda r: _response(payload={"status": "ok"}))
    assert peers.check_peer_health("https://peer.example.com") is True
    pool_kw, req = sent[0]
    assert req.url == f"https://{PUBLIC}/api/health"
    assert req.headers["Host"] == "peer.example.com"
    assert pool_kw["server_hostname"] == "peer.example.com"
    assert lookups == ["peer.example.com"]


def test_a_redirecting_peer_is_not_followed(monkeypatch, recorders):
    """A hop to a NAME would be a second, unpinned resolution. The operator
    registers the final URL instead; the redirect counts as a peer failure."""
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    sent = _capture_sends(
        monkeypatch,
        route=lambda r: _response(status=302, payload={},
                                  location="https://elsewhere.example.com/api/query-kb"))
    assert peers.query_peer_kb(PEER, "q") == []
    assert len(sent) == 1
    successes, failures = recorders
    assert len(failures) == 1 and "HTTP 302" in failures[0][1]


def test_an_unpinned_request_is_refused():
    with pytest.raises(peers.PeerURLRefused):
        peers._pinned_request("GET", "https://peer.example.com/api/health", None, timeout=1)


def test_credentials_in_a_peer_url_are_refused(monkeypatch):
    _resolver(monkeypatch, {"peer.example.com": [PUBLIC]})
    with pytest.raises(peers.PeerURLRefused):
        peers.validate_peer_url("https://user:pw@peer.example.com")
