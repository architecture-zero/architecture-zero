"""Two single-use authorities, each with exactly one winner (outside review
2026-10-06, AZ-01 and AZ-04; fixed 2026-10-07).

AZ-01, REFRESH ROTATION. The route read the token, revoked it unconditionally,
then minted and stored a successor - so two requests that both read the live
token before either revoked it each got a successor. Now one UPDATE whose WHERE
requires a live row is the consume: only the request that changed the row is
issued a successor, and the loser is treated as reuse (the family dies).

AZ-04, THE FIRST OWNER. The setup route checked owner_exists(), verified the
claim code, created the account and burned the code - so two valid claims that
both passed the check made two Owners. Now the Owner and a durable claim marker
commit in one transaction, with the Owner check repeated inside it.

Both are raced for real: two threads, each with its own client, held at a
barrier until both have passed the read the old code decided on. Each test
fails on the code before the fix.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from app.models import RefreshToken, User
from app.routers import auth as auth_route_mod

_USER = {"username": "race-user", "password": "MemberPass1"}


def _race(monkeypatch, attr, requests):
    """Run `requests` (callables taking a client) at once, holding each at
    `attr` in the auth router - a function it calls after the read the race
    is about - until all of them are there."""
    barrier = threading.Barrier(len(requests), timeout=20)
    real = getattr(auth_route_mod, attr)

    def _held(*a, **kw):
        barrier.wait()
        return real(*a, **kw)

    monkeypatch.setattr(auth_route_mod, attr, _held)
    results: list = [None] * len(requests)

    def _go(i, fn):
        results[i] = fn(TestClient(app))

    threads = [threading.Thread(target=_go, args=(i, fn)) for i, fn in enumerate(requests)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


# ── AZ-01 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def refresh_token(client, admin_headers):
    for role in ("member", "user"):
        r = client.post("/api/users", headers=admin_headers,
                        json={"current_password": "AdminPass1", **_USER, "role": role})
        if r.status_code in (200, 201, 409):
            break
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200, r.text
    return r.json()["refresh_token"]


def test_one_refresh_token_yields_one_successor_when_two_requests_race(
        client, monkeypatch, refresh_token):
    send = lambda c: c.post("/api/auth/refresh",  # noqa: E731
                            headers={"Authorization": f"Bearer {refresh_token}"})
    results = _race(monkeypatch, "refuse_if_mfa_seed_stranded", [send, send])
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 401], f"two successors from one token: {codes}"

    # The loser presented a token already consumed - reuse - so the family
    # died, the winner's successor with it.
    winner = next(r for r in results if r.status_code == 200).json()["refresh_token"]
    again = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {winner}"})
    assert again.status_code == 401


def test_the_consume_has_one_winner_at_the_database(client, refresh_token):
    from datetime import datetime, timedelta, timezone

    from app.jwt_auth import hash_token
    from app.users import rotate_refresh_token
    with get_session() as db:
        uid = db.query(RefreshToken).filter(
            RefreshToken.token_hash == hash_token(refresh_token)).one().user_id
    exp = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    barrier = threading.Barrier(4, timeout=20)
    won: list[bool] = []

    def _go(i):
        barrier.wait()
        won.append(rotate_refresh_token(hash_token(refresh_token), uid,
                                        f"successor-{i}", exp))

    threads = [threading.Thread(target=_go, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(won) == [False, False, False, True]
    with get_session() as db:
        assert db.query(RefreshToken).filter(
            RefreshToken.token_hash.like("successor-%")).count() == 1


# ── AZ-04 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def unclaimed(client):
    """Reopen the claim window for real: the suite's Owner stands down to
    admin for the test (the in-transaction check reads the database, so a
    patched owner_exists() would not reopen it), and comes back after."""
    from app import security
    with get_session() as db:
        owners = [u.id for u in db.query(User).filter(User.role == "owner").all()]
        db.query(User).filter(User.id.in_(owners)).update(
            {"role": "admin"}, synchronize_session=False)
    security._claim_code_burned = False
    try:
        yield security.setup_claim_code()
    finally:
        with get_session() as db:
            db.query(User).filter(User.role == "owner",
                                  User.id.notin_(owners)).delete(synchronize_session=False)
            db.query(User).filter(User.id.in_(owners)).update(
                {"role": "owner"}, synchronize_session=False)
        security._claim_code_burned = True


def test_two_valid_claims_racing_make_one_owner(monkeypatch, unclaimed):
    claim = lambda name: (lambda c: c.post("/api/auth/setup", json={  # noqa: E731
        "username": name, "password": "ClaimPass1", "claim_code": unclaimed}))
    results = _race(monkeypatch, "verify_setup_claim_code",
                    [claim("claimer-one"), claim("claimer-two")])
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 403], f"both claims answered: {codes}"
    with get_session() as db:
        new_owners = db.query(User).filter(
            User.role == "owner", User.username.in_(["claimer-one", "claimer-two"])).count()
    assert new_owners == 1, "two Owners from one claim window"


def test_a_claimed_deployment_stays_claimed_with_its_owner_gone(unclaimed):
    """Claimed once is claimed: with the marker in place, removing every Owner
    by hand does not reopen the bootstrap - the operator deletes the marker
    too (docs)."""
    from app.users import ClaimLost, claim_first_owner
    claim_first_owner("first-owner", "x")
    with get_session() as db:
        db.query(User).filter(User.username == "first-owner").delete()
    with pytest.raises(ClaimLost):
        claim_first_owner("second-owner", "x")
