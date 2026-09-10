"""Fleet port 2026-09-10, class 1: the per-IP auth throttle and the login
timing equalizer - in one commit, because the equalizer makes an unknown
username cost a full bcrypt round and only the throttle keeps that from being
a CPU amplifier on an anonymous route.

Every control is tested by its POSITIVE signal (the 429 fires; the hash is
paid), never just that the happy path still works - a fail-open control is
silent when off.
"""
import uuid

import pytest

from app import jwt_auth, security
from app.security import check_auth_rate_limit


# -- 1. The per-IP throttle ---------------------------------------------------

def test_throttle_fires_at_the_cap_and_is_per_ip():
    for _ in range(security.AUTH_MAX_ATTEMPTS):
        check_auth_rate_limit("198.51.100.7", "login")
    with pytest.raises(Exception) as exc:
        check_auth_rate_limit("198.51.100.7", "login")
    assert exc.value.status_code == 429
    # A different address is a different bucket - one abuser must not 429
    # every real visitor.
    check_auth_rate_limit("198.51.100.8", "login")


def test_throttle_scopes_are_separate_buckets():
    """A login-lane 429 on a shared IP must not strand MFA completions or log
    devices out via the refresh lane."""
    ip = "198.51.100.9"
    for _ in range(security.AUTH_MAX_ATTEMPTS):
        check_auth_rate_limit(ip, "login")
    with pytest.raises(Exception):
        check_auth_rate_limit(ip, "login")
    check_auth_rate_limit(ip, "mfa")       # still open
    check_auth_rate_limit(ip, "refresh")   # still open


def test_refresh_scope_has_its_own_wider_budget():
    ip = "198.51.100.10"
    for _ in range(security.REFRESH_MAX_ATTEMPTS):
        check_auth_rate_limit(ip, "refresh")
    with pytest.raises(Exception) as exc:
        check_auth_rate_limit(ip, "refresh")
    assert exc.value.status_code == 429
    assert security.REFRESH_MAX_ATTEMPTS > security.AUTH_MAX_ATTEMPTS


def test_login_route_answers_429_past_the_ip_cap(client):
    """Through the real route, with an UNKNOWN username so the per-account
    lockout never engages - what 429s here is the new per-IP bound or
    nothing. (TestClient traffic all keys to one address, which is exactly
    what makes this deterministic.)"""
    body = {"username": f"ghost-{uuid.uuid4().hex[:8]}", "password": "WrongPass1"}
    statuses = [client.post("/api/auth/login", json=body).status_code
                for _ in range(security.AUTH_MAX_ATTEMPTS + 2)]
    assert statuses[0] == 401
    assert 429 in statuses, f"the per-IP throttle never fired: {statuses}"
    first_429 = statuses.index(429)
    assert all(s == 401 for s in statuses[:first_429])
    assert all(s == 429 for s in statuses[first_429:]), \
        "once capped, the lane must stay capped for the window"


def test_mfa_route_is_throttled_before_token_work(client):
    """The mfa lane 429s past its cap even on garbage tokens - the bound runs
    before decode, so a token-less sprayer cannot iterate for free."""
    statuses = [client.post("/api/auth/mfa/complete",
                            json={"mfa_token": "not-a-token", "code": "000000"}
                            ).status_code
                for _ in range(security.AUTH_MAX_ATTEMPTS + 2)]
    assert statuses[-1] == 429


def test_refresh_route_is_throttled(client):
    statuses = [client.post("/api/auth/refresh",
                            headers={"Authorization": "Bearer not-a-token"}
                            ).status_code
                for _ in range(security.REFRESH_MAX_ATTEMPTS + 2)]
    assert statuses[0] == 401
    assert statuses[-1] == 429


# -- 2. The timing equalizer --------------------------------------------------

def _count_verifies(monkeypatch):
    calls = []
    real = jwt_auth.verify_password

    def counting(plain, hashed):
        calls.append(hashed)
        return real(plain, hashed)
    monkeypatch.setattr(jwt_auth, "verify_password", counting)
    return calls


def test_unknown_username_pays_one_bcrypt_verify_against_the_dummy(monkeypatch):
    calls = _count_verifies(monkeypatch)
    assert jwt_auth.authenticate_user(f"ghost-{uuid.uuid4().hex[:8]}", "x") is None
    assert calls == [jwt_auth._DUMMY_PASSWORD_HASH], (
        "the missing-user branch must pay exactly the verify a known user pays")


def test_the_dummy_hash_matches_the_live_work_factor_and_verifies_nothing():
    """bcrypt encodes its cost in the hash: `$2b$12$...`. The dummy only
    equalizes if its cost equals a freshly minted hash's - which is why it is
    built through hash_password at import rather than pasted as a literal."""
    fresh = jwt_auth.hash_password("throwaway")
    assert fresh.split("$")[2] == jwt_auth._DUMMY_PASSWORD_HASH.split("$")[2]
    assert jwt_auth.verify_password("", jwt_auth._DUMMY_PASSWORD_HASH) is False
    assert jwt_auth.verify_password("throwaway", jwt_auth._DUMMY_PASSWORD_HASH) is False


def test_login_route_reaches_the_equalizer_for_an_unknown_username(client, monkeypatch):
    """The ROUTE pin, not just the function's: `not user or not
    authenticate_user(...)` short-circuited, so the equalizer inside the
    function never ran for an unknown username through the real endpoint."""
    calls = _count_verifies(monkeypatch)
    r = client.post("/api/auth/login",
                    json={"username": f"ghost-{uuid.uuid4().hex[:8]}", "password": "WrongPass1"})
    assert r.status_code == 401
    assert calls == [jwt_auth._DUMMY_PASSWORD_HASH], (
        f"the login route skipped the bcrypt round for an unknown username: {calls}")
