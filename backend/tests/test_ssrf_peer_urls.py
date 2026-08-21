"""SSRF guard on Eco Mode peer URLs.

A peer URL is an operator-supplied address that the SERVER then fetches, which
is the textbook SSRF shape. Owner-only configuration bounds who can aim the
request, not what it can reach - and the most valuable target needs no
credentials at all: 169.254.169.254 hands out cloud IAM credentials to anything
that asks from inside the instance.

The guard resolves the host rather than pattern-matching the string, because
an attacker controls DNS for a name they own and 'peer.example.com' can resolve
straight back to link-local.
"""
import socket
from unittest.mock import patch

import pytest

from app.peers import PeerURLRefused, validate_peer_url


def _resolves_to(ip: str):
    """Pin getaddrinfo so these tests never depend on real DNS."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return patch("app.peers.socket.getaddrinfo",
                 return_value=[(family, socket.SOCK_STREAM, 6, "", (ip, 443))])


# -- Refused ------------------------------------------------------------------

@pytest.mark.parametrize("ip,label", [
    ("169.254.169.254", "cloud metadata service"),
    ("169.254.1.1",     "link-local"),
    ("127.0.0.1",       "loopback"),
    ("::1",             "loopback v6"),
    ("10.0.0.5",        "private 10/8"),
    ("172.16.4.4",      "private 172.16/12"),
    ("192.168.1.10",    "private 192.168/16"),
    ("0.0.0.0",         "unspecified"),
])
def test_refuses_internal_targets(ip, label):
    with _resolves_to(ip):
        with pytest.raises(PeerURLRefused):
            validate_peer_url("https://peer.example.com")


def test_refuses_a_public_name_that_resolves_inward():
    """The case string matching cannot catch, and the reason we resolve."""
    with _resolves_to("169.254.169.254"):
        with pytest.raises(PeerURLRefused) as e:
            validate_peer_url("https://totally-legit-peer.example.com")
        assert "link-local" in str(e.value)


def test_refuses_when_any_resolved_address_is_internal():
    """Which address requests picks is not ours to choose, so all must pass."""
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ]
    with patch("app.peers.socket.getaddrinfo", return_value=infos):
        with pytest.raises(PeerURLRefused):
            validate_peer_url("https://peer.example.com")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://peer.example.com",
    "ftp://peer.example.com",
    "",
    "   ",
])
def test_refuses_non_http_schemes_and_empty(url):
    with pytest.raises(PeerURLRefused):
        validate_peer_url(url)


def test_refuses_a_host_that_does_not_resolve():
    with patch("app.peers.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
        with pytest.raises(PeerURLRefused):
            validate_peer_url("https://no-such-peer.example.com")


# -- Allowed ------------------------------------------------------------------

def test_allows_a_public_peer():
    with _resolves_to("93.184.216.34"):
        assert validate_peer_url("https://peer.example.com/") == "https://peer.example.com"


def test_private_peers_allowed_when_the_operator_opts_in():
    """A LAN peer is a real deployment, so the private-range refusal is a
    default rather than a law. Loopback and link-local are NOT re-permitted by
    the opt-in - neither is ever a peer, and one of them is the metadata
    address."""
    with _resolves_to("10.0.0.5"), patch("app.peers._ALLOW_PRIVATE", True):
        assert validate_peer_url("http://lan-peer.internal") == "http://lan-peer.internal"
    with _resolves_to("127.0.0.1"), patch("app.peers._ALLOW_PRIVATE", True):
        with pytest.raises(PeerURLRefused):
            validate_peer_url("http://localhost:8000")


# -- Wired into the fetch paths, not only config ------------------------------

def test_query_peer_kb_refuses_a_stored_bad_row_without_fetching():
    """Rows predating the guard still reach the fetch path. It must not
    fetch first and validate later."""
    from app import peers as peers_mod
    peer = {"id": "legacy", "name": "legacy", "url": "http://169.254.169.254"}
    with patch.object(peers_mod, "_req") as req, \
         patch.object(peers_mod, "_circuit_open", return_value=False), \
         patch.object(peers_mod, "_record_failure") as rec:
        assert peers_mod.query_peer_kb(peer, "anything") == []
        req.get.assert_not_called()
        rec.assert_called_once()


def test_check_peer_health_refuses_without_fetching():
    from app import peers as peers_mod
    with patch.object(peers_mod, "_req") as req:
        assert peers_mod.check_peer_health("http://169.254.169.254") is False
        req.get.assert_not_called()


def test_add_peer_endpoint_rejects_ssrf_url(client, admin_headers):
    with _resolves_to("169.254.169.254"):
        r = client.post("/api/peers",
                        json={"id": "evil", "name": "evil",
                              "url": "http://metadata.example.com", "model": "m",
                              "enabled": True},
                        headers=admin_headers)
    assert r.status_code == 400, r.text
