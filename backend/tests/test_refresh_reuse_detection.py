"""Fleet port 2026-09-10, class 3: refresh-token reuse detection.

Rotation marks the old row revoked; a revoked token coming back means a
stolen copy (or a harmless stale client) and kills the whole family. Tested
by its POSITIVE signal - the successor token dies - never just that rotation
still works.
"""
import uuid

import pytest

_USER = {"username": "reuse-user", "password": "MemberPass1"}


def _login(client, creds):
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, f"login failed: {r.text}"
    return r.json()


def _create(client, admin_headers, creds):
    # The lowest-privilege role name differs between builds: some use
    # owner/admin/member, others admin/manager/user. Ask for whichever the
    # instance accepts rather than hardcoding one.
    for role in ("member", "user"):
        r = client.post("/api/users", headers=admin_headers,
                        json={"current_password": "AdminPass1", **creds, "role": role})
        if r.status_code in (200, 201, 409):
            return
    raise AssertionError(f"no accepted role: {r.text}")


@pytest.fixture
def member_tokens(client, admin_headers):
    _create(client, admin_headers, _USER)
    return _login(client, _USER)


def test_replayed_rotated_refresh_token_kills_the_family(client, member_tokens):
    r1 = member_tokens["refresh_token"]

    # Normal rotation: r1 spends itself and mints r2.
    rot = client.post("/api/auth/refresh",
                      headers={"Authorization": f"Bearer {r1}"})
    assert rot.status_code == 200, rot.text
    r2 = rot.json()["refresh_token"]

    # The replay: r1 comes back. 401 - with the SAME detail as a garbage
    # token, so the response is not an oracle for "this hash was once real".
    replay = client.post("/api/auth/refresh",
                         headers={"Authorization": f"Bearer {r1}"})
    assert replay.status_code == 401
    garbage = client.post("/api/auth/refresh",
                          headers={"Authorization": "Bearer not-a-real-token"})
    assert garbage.status_code == 401
    assert replay.json()["detail"] == garbage.json()["detail"]

    # The family is dead: r2 - never itself replayed - is revoked too.
    after = client.post("/api/auth/refresh",
                        headers={"Authorization": f"Bearer {r2}"})
    assert after.status_code == 401, \
        "family revocation did not reach the successor token"


def test_garbage_refresh_token_revokes_nothing(client, admin_headers):
    """The reuse path must key on a REAL rotated row - an invented token 401s
    without touching anyone's sessions."""
    creds = {"username": f"reuse-{uuid.uuid4().hex[:8]}", "password": "KeepPass1"}
    _create(client, admin_headers, creds)
    live = _login(client, creds)["refresh_token"]

    assert client.post("/api/auth/refresh",
                       headers={"Authorization": "Bearer never-was-a-token"}
                       ).status_code == 401
    still = client.post("/api/auth/refresh",
                        headers={"Authorization": f"Bearer {live}"})
    assert still.status_code == 200, "an unrelated 401 revoked a live session"


def test_a_failed_redis_delete_on_revocation_is_logged_not_swallowed(monkeypatch):
    """The one condition under which a revoked token keeps working for its
    cache TTL: the Redis key survives the revoke. It must at least be loud."""
    from app import users

    class _Boom:
        def delete(self, *a):
            raise RuntimeError("redis down")
    monkeypatch.setattr("app.redis_client.get_redis", lambda: _Boom())
    seen = []
    monkeypatch.setattr("app.logger.log", lambda event, **kw: seen.append((event, kw)))
    users.revoke_refresh_token("no-such-hash")
    assert any(e == "refresh_redis_delete_failed" and kw.get("where") == "revoke_refresh_token"
               for e, kw in seen), seen
