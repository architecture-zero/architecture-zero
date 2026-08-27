"""MFA challenge guard - the strongest door was the only one with no lock counter.

Found 2026-08-27 by an outside review of the repo and verified in code before
being believed. /api/auth/mfa/complete had NO attempt counter, NO lockout, and
NO invalidation of the challenge after use - while /api/auth/login, twenty lines
above it, has all three.

The consequence was concrete, not theoretical: a valid mfa_token was an
unbounded guessing permit for its whole 5-minute life against a 6-digit code,
and pyotp's valid_window=1 makes three codes acceptable at any instant. Nothing
stopped a caller from spending those five minutes, and re-logging in minted a
fresh window. The same token also stayed usable after a successful login.

These tests pin the three bounds that now apply, in the order they can fail:
single-use, per-challenge attempts, per-account lockout. The last one matters
most: it deliberately reuses the SAME failed_attempts/locked_until the password
path uses, because two different lockout policies on one account is how one of
them becomes the weaker one nobody remembers.
"""
import pyotp
import pytest

from app import main, security
from app.jwt_auth import create_mfa_challenge_token
from app.users import reset_failed_attempts

_MFA_USER = {"username": "mfa_probe", "password": "MfaProbe1"}


@pytest.fixture
def mfa_user(client, admin_headers):
    """A user with MFA enabled and a known TOTP secret, reset between tests."""
    from app.users import get_user_by_username
    from app.db import get_session
    from app.models import User

    existing = get_user_by_username(_MFA_USER["username"])
    if not existing:
        # The lowest-privilege role name diverges across the fleet on purpose:
        # Kin's ladder is owner/admin/member, the forks' is admin/manager/user.
        # This file is kept identical in all four repos, so it asks for whichever
        # the instance accepts rather than hardcoding one and erroring on three.
        for role in ("user", "member"):
            r = client.post("/api/users", json={**_MFA_USER, "role": role},
                            headers=admin_headers)
            if r.status_code in (200, 201):
                break
        assert r.status_code in (200, 201), f"no accepted role: {r.text}"
        existing = get_user_by_username(_MFA_USER["username"])

    secret = pyotp.random_base32()
    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": True, "mfa_secret": secret,
             "failed_attempts": 0, "locked_until": None})

    yield {"id": existing["id"], "secret": secret}

    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": False, "mfa_secret": None,
             "failed_attempts": 0, "locked_until": None})


def _complete(client, token, code):
    return client.post("/api/auth/mfa/complete",
                       json={"mfa_token": token, "code": code})


# ── 1. Single use - the challenge is burned on success ───────────────────────

def test_challenge_cannot_be_replayed_after_success(client, mfa_user):
    """The bug that made the token a standing credential: it kept working."""
    tok = create_mfa_challenge_token(mfa_user["id"])
    good = pyotp.TOTP(mfa_user["secret"]).now()

    first = _complete(client, tok, good)
    assert first.status_code == 200, first.text

    replay = _complete(client, tok, good)
    assert replay.status_code == 401
    assert "already been completed" in replay.json()["detail"]


# ── 2. Per-challenge attempt cap ─────────────────────────────────────────────

def test_wrong_codes_exhaust_the_challenge(client, mfa_user, monkeypatch):
    """A single sign-in gets a small number of tries, then that challenge dies -
    which is the bound that actually stops grinding a live token."""
    monkeypatch.setattr(security, "MFA_MAX_ATTEMPTS", 3)
    tok = create_mfa_challenge_token(mfa_user["id"])

    codes = [_complete(client, tok, "000000").status_code for _ in range(4)]

    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429


def test_exhausted_challenge_refuses_even_the_CORRECT_code(client, mfa_user,
                                                           monkeypatch):
    """The bound has to hold against a right answer too, or it only delays the
    attacker until they land one."""
    monkeypatch.setattr(security, "MFA_MAX_ATTEMPTS", 2)
    tok = create_mfa_challenge_token(mfa_user["id"])

    _complete(client, tok, "000000")
    _complete(client, tok, "000000")

    good = pyotp.TOTP(mfa_user["secret"]).now()
    assert _complete(client, tok, good).status_code == 429


# ── 3. Per-account lockout - the bound that survives fresh challenges ────────

def test_failures_drive_the_SAME_account_lockout_as_the_password_path(
        client, mfa_user, monkeypatch):
    """Per-challenge limits alone are defeated by requesting a new challenge.
    MFA failures must count on the ACCOUNT, using the existing machinery."""
    monkeypatch.setattr(main, "MAX_LOGIN_ATTEMPTS", 3)
    monkeypatch.setattr(security, "MFA_MAX_ATTEMPTS", 1)

    # A fresh challenge each time - exactly the move a per-challenge cap misses.
    last = None
    for _ in range(3):
        tok = create_mfa_challenge_token(mfa_user["id"])
        last = _complete(client, tok, "000000")

    assert last.status_code == 429
    assert "locked" in last.json()["detail"].lower()


def test_locked_account_cannot_complete_mfa_with_a_correct_code(
        client, mfa_user, monkeypatch):
    """This endpoint used to bypass lockout entirely, so a locked account holding
    a live challenge could still walk in."""
    from datetime import datetime, timezone, timedelta
    from app.users import lock_user

    lock_user(mfa_user["id"],
              (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
    tok = create_mfa_challenge_token(mfa_user["id"])
    good = pyotp.TOTP(mfa_user["secret"]).now()

    r = _complete(client, tok, good)

    assert r.status_code == 429
    assert "locked" in r.json()["detail"].lower()
    reset_failed_attempts(mfa_user["id"])


def test_success_clears_the_failure_counter(client, mfa_user):
    """Otherwise a user who fat-fingers a code twice carries that toward a
    lockout on some unrelated future day."""
    from app.users import get_user_by_id

    tok = create_mfa_challenge_token(mfa_user["id"])
    _complete(client, tok, "000000")
    assert get_user_by_id(mfa_user["id"])["failed_attempts"] > 0

    tok2 = create_mfa_challenge_token(mfa_user["id"])
    assert _complete(client, tok2, pyotp.TOTP(mfa_user["secret"]).now()).status_code == 200
    assert get_user_by_id(mfa_user["id"])["failed_attempts"] == 0


# ── 4. Tokens minted before the fix cannot be honoured ───────────────────────

def test_challenge_without_a_jti_is_refused(client, mfa_user):
    """Fail-closed on the migration edge. A pre-2026-08-27 token has no jti, so
    it can be neither burned nor counted - honouring it would leave open exactly
    the hole this closes. Cost is one 5-minute window at deploy."""
    from datetime import datetime, timezone, timedelta
    from jose import jwt
    from app.jwt_auth import SECRET_KEY, ALGORITHM

    legacy = jwt.encode(
        {"sub": str(mfa_user["id"]), "type": "mfa",
         "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        SECRET_KEY, algorithm=ALGORITHM)

    r = _complete(client, legacy, pyotp.TOTP(mfa_user["secret"]).now())
    assert r.status_code == 401


# ── 5. The guard's own store stays bounded ──────────────────────────────────

def test_challenge_store_is_swept(monkeypatch):
    """In-process state on an unauthenticated-adjacent path has to prune, or it
    is a slow memory leak keyed by anyone who can trigger a login."""
    monkeypatch.setattr(security, "MFA_CHALLENGE_TTL", 0)
    security._mfa_challenges["stale"] = {"attempts": 1, "used": False, "ts": 0.0}

    security.check_mfa_challenge("fresh")

    assert "stale" not in security._mfa_challenges
