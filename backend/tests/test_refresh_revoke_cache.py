"""Revocation must reach the Redis copy of a refresh token, not only the DB
flag: get_refresh_token reads Redis FIRST and never re-checks the DB
`revoked` column, so a DB-only revoke leaves a signed-out session valid
until its TTL. Found by the 2026-09-10 T9 review on the surfaces whose by-id
revoke flipped the flag only; the suite had never exercised the Redis half
because REDIS_URL is unset under pytest. This fake stands in for Redis.
"""
import pytest

_USER = {"username": "revoke_cache_probe", "password": "RevokeCache#2026x"}


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, k):
        return self.store.get(k)

    def setex(self, k, ttl, v):
        self.store[k] = v

    def set(self, k, v, **kw):
        self.store[k] = v

    def delete(self, *keys):
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def keys(self, pattern="*"):
        return list(self.store)


@pytest.fixture
def fake_redis(monkeypatch):
    import app.redis_client as rc
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def probe(client, admin_headers):
    from app.users import get_user_by_username
    if not get_user_by_username(_USER["username"]):
        for role in ("user", "member"):
            r = client.post("/api/users", json={"current_password": "AdminPass1", **_USER, "role": role},
                            headers=admin_headers)
            if r.status_code in (200, 201):
                break
        assert r.status_code in (200, 201), r.text
    return get_user_by_username(_USER["username"])


def _login(client):
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200, r.text
    return r.json()


def _cached(fake):
    return [k for k in fake.keys() if k.startswith("az:rt:")]


def test_single_session_revoke_drops_the_cached_copy(client, probe, fake_redis):
    tok = _login(client)
    assert len(_cached(fake_redis)) == 1, "login did not cache the refresh token"
    hdr = {"Authorization": f"Bearer {tok['access_token']}"}
    sessions = client.get("/api/auth/sessions", headers=hdr).json()["sessions"]
    assert sessions, "no session listed"
    r = client.delete(f"/api/auth/sessions/{sessions[-1]['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    assert _cached(fake_redis) == [], "the cached copy survived a by-id revoke"
    r = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {tok['refresh_token']}"})
    assert r.status_code == 401, r.text


def test_family_revoke_is_total_even_after_a_by_id_flag(client, probe, fake_redis):
    a = _login(client)
    b = _login(client)
    assert len(_cached(fake_redis)) == 2
    hdr = {"Authorization": f"Bearer {b['access_token']}"}
    sessions = client.get("/api/auth/sessions", headers=hdr).json()["sessions"]
    # Flag one row in the DB only, the way the old by-id revoke did, to prove the
    # family revoke no longer skips a row that is already flagged.
    from app.db import get_session
    from app.models import RefreshToken
    with get_session() as db:
        db.query(RefreshToken).filter(RefreshToken.id == sessions[0]["id"]).update({"revoked": True})
    assert len(_cached(fake_redis)) == 2, "precondition: the flagged row is still cached"
    r = client.delete("/api/auth/sessions", headers=hdr)
    assert r.status_code == 200, r.text
    assert _cached(fake_redis) == [], "a cached copy survived sign-out-everywhere"
    for tok in (a, b):
        r = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {tok['refresh_token']}"})
        assert r.status_code == 401, r.text
