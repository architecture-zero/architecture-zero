"""A3-4: the account lockout is not an existence oracle (ruled 2026-09-13).

The login door used to answer a locked account in its own voice - 429 "Too
many failed attempts. Account locked for N minutes." on the attempt that
locks it, then 429 "Account locked. Try again in N minute(s)." until the
window passes - while a username that does not exist answered 401 forever.
Status and body therefore told an anonymous caller which usernames are real,
at five requests each, inside whatever the per-IP throttle allows.

T11 had already equalized the TIMING of that door (an unknown username pays a
bcrypt round against a dummy hash). This closes the other half: the ANSWER.
A locked account now returns the same 401, with the same body, as a wrong
password and as a username that was never registered.

What did NOT change, and is pinned below so nobody "simplifies" it back:
  - the lock is still enforced (a CORRECT password while locked is refused)
  - failed_attempts / locked_until bookkeeping is untouched
  - the locked path still pays its bcrypt round, so the answer cannot be
    told apart by timing either (a pre-bcrypt 401 would have reopened T11)
  - the per-IP throttle keeps its own 429; it is keyed by IP, not username,
    so it is not an oracle
  - the step-up door and /api/auth/mfa/complete keep their 429s: both are
    reachable only by a caller who already proved the password, so neither
    tells an attacker anything about which accounts exist

Accepted cost, taken deliberately 2026-09-13: a locked-out legitimate user reads
"invalid credentials" instead of "try again in N minutes". The refusal is
recorded server-side instead, which the last test here pins.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import jwt_auth
from app.jwt_auth import MAX_LOGIN_ATTEMPTS, hash_password
from app.routers import auth as login_module
from app.users import (create_user, get_user_by_id, get_user_by_username,
                       lock_user, reset_failed_attempts, unlock_user)

PW = "OracleP1ass"
WRONG = "not-the-password"


def _mkuser(prefix):
    """A throwaway account, created straight in the store - the API route
    would spend the per-IP auth budget this file needs for real attempts."""
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    uid = create_user(name, hash_password(PW))
    reset_failed_attempts(uid)
    return name, uid


def _login(client, username, password):
    return client.post("/api/auth/login",
                       json={"username": username, "password": password})


def _answer(r):
    """The pair an attacker can actually observe."""
    return r.status_code, r.json()


@pytest.fixture
def locked_account():
    """An account locked for a full window, unlocked again afterwards."""
    name, uid = _mkuser("lockoracle")
    until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    lock_user(uid, until)
    try:
        yield name, uid
    finally:
        unlock_user(uid)
        reset_failed_attempts(uid)


def test_a_locked_account_answers_exactly_like_a_wrong_password_and_a_ghost(
        client, locked_account):
    """(a) and (b): one answer for all three cases, body included."""
    name, uid = locked_account
    live, _ = _mkuser("lockcontrol")
    try:
        locked_right_pw = _answer(_login(client, name, PW))
        locked_wrong_pw = _answer(_login(client, name, WRONG))
        unlocked_wrong_pw = _answer(_login(client, live, WRONG))
        ghost = _answer(_login(client, f"ghost-{uuid.uuid4().hex[:8]}", WRONG))

        assert locked_right_pw == (401, {"detail": "Invalid username or password"})
        assert locked_wrong_pw == locked_right_pw
        assert unlocked_wrong_pw == locked_right_pw, (
            "a locked account is distinguishable from a wrong password")
        assert ghost == locked_right_pw, (
            "a locked account is distinguishable from a username that does not exist")
    finally:
        ghost_user = get_user_by_username(live)
        if ghost_user:
            unlock_user(ghost_user["id"])
            reset_failed_attempts(ghost_user["id"])


def test_the_attempt_that_locks_the_account_answers_the_same_401(client):
    """(a) at the transition - the LOUDER half of the oracle.

    The Nth wrong password used to flip 401 into 429, so five requests
    confirmed a username outright. Every attempt in the run now answers
    identically, and the lock is still written on the last one.
    """
    name, uid = _mkuser("lockflip")
    try:
        answers = [_answer(_login(client, name, WRONG))
                   for _ in range(MAX_LOGIN_ATTEMPTS)]
        assert answers == [(401, {"detail": "Invalid username or password"})] * MAX_LOGIN_ATTEMPTS, (
            f"the run is not uniform, so it still marks a real username: {answers}")
        assert get_user_by_id(uid).get("locked_until"), (
            "the answer was equalized by dropping the lock itself")
        assert get_user_by_id(uid)["failed_attempts"] >= MAX_LOGIN_ATTEMPTS
    finally:
        unlock_user(uid)
        reset_failed_attempts(uid)


def test_the_lock_still_lifts_when_its_window_passes(client):
    """(c): the refusal is silent, not permanent. An expired lock clears and
    the correct password gets in - the expiry branch also re-reads the row,
    which is why the login below returns a real token rather than 401."""
    name, uid = _mkuser("lockexpiry")
    try:
        lock_user(uid, (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
        assert _login(client, name, PW).status_code == 401

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        lock_user(uid, past)
        r = _login(client, name, PW)
        assert r.status_code == 200, r.text
        assert r.json()["access_token"]
        assert get_user_by_id(uid).get("locked_until") is None
    finally:
        unlock_user(uid)
        reset_failed_attempts(uid)


def test_the_locked_path_still_pays_its_bcrypt_round(client, locked_account,
                                                     monkeypatch):
    """The T11 leg. The lockout branch used to raise BEFORE the password was
    ever checked, so a locked account answered in about a millisecond while
    every other refusal paid a full bcrypt round. Answering 401 from that same
    pre-bcrypt position would have swapped a status-code oracle for a timing
    one. The check is read early and answered late, so the hash still runs.
    """
    name, uid = locked_account
    calls = []
    real = jwt_auth.verify_password

    def counting(plain, hashed):
        calls.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr(jwt_auth, "verify_password", counting)

    assert _login(client, name, WRONG).status_code == 401
    assert calls == [get_user_by_id(uid)["password_hash"]], (
        f"the locked path skipped bcrypt, which is a timing oracle: {calls}")


def test_the_refused_attempt_is_recorded_server_side(client, locked_account,
                                                     monkeypatch):
    """(d): the caller is no longer told the lock refused them, so the
    operator must be. Without this line a locked account under attack looks
    exactly like ordinary wrong-password noise in the log."""
    name, uid = locked_account
    seen = []
    real = login_module.log
    monkeypatch.setattr(login_module, "log",
                        lambda event, **kw: (seen.append((event, kw)), real(event, **kw))[0])

    assert _login(client, name, PW).status_code == 401

    refusals = [kw for event, kw in seen if event == "auth_login_refused_locked"]
    assert len(refusals) == 1, seen
    assert refusals[0]["user_id"] == uid
    assert refusals[0]["username"] == name
    assert isinstance(refusals[0]["minutes"], int) and refusals[0]["minutes"] > 0


def test_all_three_refusals_move_the_failure_counter_by_one(client, locked_account):
    """The side channel the response parity alone does not close.

    az_auth_failures_total is a counter the login route bumps on a refusal. If
    the locked path skips it, the counter moves on a wrong password and on an
    unknown username but NOT on a locked account - so reading /metrics either
    side of a single request separates the three cases the 401 just equalized.
    That read is anonymous wherever /metrics has no route-level gate, which is
    the shipped default on more than one surface in this fleet.

    Found by attacking this change before it shipped (2026-09-14), not after.
    """
    from app.metrics import get_snapshot

    name, uid = locked_account
    live, live_uid = _mkuser("lockcounter")
    try:
        def _delta(username, password):
            before = get_snapshot()["auth_failures_total"]
            assert _login(client, username, password).status_code == 401
            return get_snapshot()["auth_failures_total"] - before

        locked_delta = _delta(name, PW)
        wrong_delta = _delta(live, WRONG)
        ghost_delta = _delta(f"ghost-{uuid.uuid4().hex[:8]}", WRONG)

        assert locked_delta == wrong_delta == ghost_delta == 1, (
            f"the counter separates the three refusals: locked={locked_delta} "
            f"wrong={wrong_delta} ghost={ghost_delta}")
    finally:
        unlock_user(live_uid)
        reset_failed_attempts(live_uid)


def test_the_per_ip_throttle_keeps_its_own_429(client):
    """The 429 that STAYS. It is keyed by IP and counts attempts rather than
    failures, so it fires for an unknown username too and reveals nothing.
    Pinned here because the obvious way to read this file is "A3-4 deleted the
    429s at the login door", and deleting this one would remove the only bound
    on anonymous bcrypt work at an unauthenticated route.
    """
    from app import security
    statuses = [_login(client, f"ghost-{uuid.uuid4().hex[:8]}", WRONG).status_code
                for _ in range(security.AUTH_MAX_ATTEMPTS + 1)]
    assert statuses[0] == 401
    assert statuses[-1] == 429, statuses
    assert all(s in (401, 429) for s in statuses)
