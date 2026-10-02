"""The authenticator code on re-key, and one bound on code guesses (2026-10-02).

Three facts pinned here, the template first (the fleet order):

1. Re-keying the authenticator takes the CURRENT code as well as the password.
   Until now the password alone re-keyed an enrolled account (the 2026-09-16
   step-up), so whoever held a session AND the password could swap in their
   own authenticator. Enrolling an account that has no factor still takes the
   password alone - there is no code to ask for yet.

2. /api/auth/mfa/enable checks a PENDING code only. On an enrolled account it
   verified the code against the LIVE seed and answered 200 for a right one and
   401 for a wrong one, with no counter and no throttle: an unbounded oracle for
   the current code, open to a session alone. Every door that accepts a current
   code was only as strong as that oracle. It now answers 409 on an enrolled
   account, the same answer for any code.

3. Wrong codes count per ACCOUNT, in a bound only a right code clears. The
   account lock (failed_attempts) cannot be that bound: a correct password
   clears it at every password step-up door, so a session-plus-password holder
   could guess a few codes, pass one step-up, and guess again, for as long as
   the per-IP throttle allowed from as many addresses as they had. Sign-in and
   the re-key share the bound, because a code learned at one is good at the
   other for its 30 to 90 seconds.
"""
import time

import pyotp
import pytest

from app import security

_USER = {"username": "totp_bound_probe", "password": "TotpBound#2026x"}


@pytest.fixture
def probe(client, admin_headers):
    """A member with a known enrolled seed and a live session.

    Signed in BEFORE the factor is switched on, so the password path hands the
    session straight back; the access token outlives the flips each test makes.
    """
    from app.users import get_user_by_username
    from app.db import get_session
    from app.models import User

    existing = get_user_by_username(_USER["username"])
    if not existing:
        # Role names differ between builds (test_mfa_challenge_guard.py's
        # fixture, which this mirrors) - ask for whichever the instance takes.
        for role in ("user", "member"):
            r = client.post("/api/users", headers=admin_headers,
                            json={"current_password": "AdminPass1", **_USER, "role": role})
            if r.status_code in (200, 201):
                break
        assert r.status_code in (200, 201), f"no accepted role: {r.text}"
        existing = get_user_by_username(_USER["username"])

    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": False, "mfa_secret": None,
             "failed_attempts": 0, "locked_until": None})
    r = client.post("/api/auth/login", json=_USER)
    assert r.status_code == 200 and "access_token" in r.json(), r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    secret = pyotp.random_base32()
    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": True, "mfa_secret": secret})

    yield {"id": existing["id"], "headers": headers, "secret": secret}

    with get_session() as db:
        db.query(User).filter(User.id == existing["id"]).update(
            {"mfa_enabled": False, "mfa_secret": None,
             "failed_attempts": 0, "locked_until": None})


def _row(user_id):
    from app.users import get_user_by_id
    return get_user_by_id(user_id)


def _wrong_code(secret):
    """A code none of the three accepted steps (now, and one either side) matches,
    so no test here can pass by a one-in-three-hundred-thousand accident."""
    totp = pyotp.TOTP(secret)
    live = {totp.at(time.time() + step) for step in (-30, 0, 30)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in live)


def _rekey(client, probe, **extra):
    return client.post("/api/auth/mfa/setup", headers=probe["headers"],
                       json={"rekey": True, "current_password": _USER["password"], **extra})


def _sign_in(client, code):
    login = client.post("/api/auth/login", json=_USER)
    assert login.status_code == 200 and login.json().get("mfa_required"), login.text
    return client.post("/api/auth/mfa/complete",
                       json={"mfa_token": login.json()["mfa_token"], "code": code})


# -- 1. The re-key takes the current code -------------------------------------

def test_rekey_takes_the_current_code_as_well_as_the_password(client, probe):
    seed = probe["secret"]

    # The password alone - what re-keyed an enrolled account until today.
    r = _rekey(client, probe)
    assert r.status_code == 400 and isinstance(r.json()["detail"], str), r.text
    assert "authenticator code" in r.json()["detail"].lower()
    row = _row(probe["id"])
    assert row["mfa_secret"] == seed and row["mfa_enabled"], "a refused re-key touched the seed"

    r = _rekey(client, probe, mfa_code=_wrong_code(seed))
    assert r.status_code == 400 and r.json()["detail"] == "Invalid authenticator code", r.text
    row = _row(probe["id"])
    assert row["mfa_secret"] == seed and row["mfa_enabled"]

    r = _rekey(client, probe, mfa_code=pyotp.TOTP(seed).now())
    assert r.status_code == 200, r.text
    row = _row(probe["id"])
    assert row["mfa_secret"] == r.json()["secret"] != seed
    assert row["mfa_enabled"] is False, "off until the new code verifies, as before"


def test_the_rekey_finishes_on_the_new_code(client, probe):
    r = _rekey(client, probe, mfa_code=pyotp.TOTP(probe["secret"]).now())
    assert r.status_code == 200, r.text
    fresh = r.json()["secret"]
    r = client.post("/api/auth/mfa/enable", headers=probe["headers"],
                    json={"code": pyotp.TOTP(fresh).now()})
    assert r.status_code == 200, r.text
    assert _row(probe["id"])["mfa_enabled"] is True


def test_first_enrollment_still_takes_the_password_alone(client, probe):
    from app.db import get_session
    from app.models import User
    with get_session() as db:
        db.query(User).filter(User.id == probe["id"]).update(
            {"mfa_enabled": False, "mfa_secret": None})
    r = client.post("/api/auth/mfa/setup", headers=probe["headers"],
                    json={"current_password": _USER["password"]})
    assert r.status_code == 200, r.text


def test_a_stranded_seed_cannot_be_rekeyed_by_a_code_it_cannot_check(client, probe):
    """Enabled, and the seed unreadable under this instance's key (T9): the
    code cannot be checked, so the re-key refuses with the remedy rather than
    500 on pyotp.TOTP(None) or overwriting the factor on the password alone."""
    from cryptography.fernet import Fernet
    from app.db import get_session
    from app.models import User
    raw = Fernet(Fernet.generate_key()).encrypt(pyotp.random_base32().encode()).decode()
    with get_session() as db:
        db.query(User).filter(User.id == probe["id"]).update({"mfa_secret": raw})
    r = _rekey(client, probe, mfa_code="123456")
    assert r.status_code == 400, r.text
    assert "rekey_at_rest.py" in r.json()["detail"]
    with get_session() as db:
        assert db.query(User.mfa_secret).filter(User.id == probe["id"]).scalar() == raw


# -- 2. Enable checks a pending code only --------------------------------------

def test_enable_on_an_enrolled_account_checks_no_code(client, probe):
    seed = probe["secret"]
    answers = []
    for code in (pyotp.TOTP(seed).now(), _wrong_code(seed)):
        r = client.post("/api/auth/mfa/enable", headers=probe["headers"], json={"code": code})
        answers.append((r.status_code, r.json()["detail"]))
    assert answers[0] == answers[1], "a right and a wrong code must read alike"
    assert answers[0][0] == 409, answers
    row = _row(probe["id"])
    assert row["mfa_secret"] == seed and row["mfa_enabled"]


# -- 3. One bound on code guesses, per account ---------------------------------

def test_a_correct_password_does_not_refund_code_guesses(client, probe, monkeypatch):
    monkeypatch.setattr(security, "TOTP_MAX_FAILURES", 3)
    seed = probe["secret"]
    for _ in range(3):
        r = _rekey(client, probe, mfa_code=_wrong_code(seed))
        assert r.status_code == 400, r.text
    # Each of those passed the password step-up, which clears failed_attempts
    # before the code is read - so the account lock never got past one.
    assert _row(probe["id"])["failed_attempts"] == 1
    r = _rekey(client, probe, mfa_code=pyotp.TOTP(seed).now())
    assert r.status_code == 429, r.text
    row = _row(probe["id"])
    assert row["mfa_secret"] == seed and row["mfa_enabled"]


def test_sign_in_and_rekey_share_one_bound(client, probe, monkeypatch):
    monkeypatch.setattr(security, "TOTP_MAX_FAILURES", 3)
    seed = probe["secret"]
    # Two wrong codes at sign-in, each on a fresh challenge (a re-login).
    for _ in range(2):
        assert _sign_in(client, _wrong_code(seed)).status_code == 401
    # One at the re-key door, behind a correct password.
    assert _rekey(client, probe, mfa_code=_wrong_code(seed)).status_code == 400
    # Now even the right code is refused at sign-in.
    r = _sign_in(client, pyotp.TOTP(seed).now())
    assert r.status_code == 429, r.text
    assert "access_token" not in r.json()


def test_a_right_code_clears_the_bound(client, probe, monkeypatch):
    monkeypatch.setattr(security, "TOTP_MAX_FAILURES", 3)
    seed = probe["secret"]
    assert _sign_in(client, _wrong_code(seed)).status_code == 401
    assert _sign_in(client, _wrong_code(seed)).status_code == 401
    assert _sign_in(client, pyotp.TOTP(seed).now()).status_code == 200
    # Cleared, not merely under the cap: two more wrong codes read as wrong
    # codes, and the right one still signs in.
    assert _sign_in(client, _wrong_code(seed)).status_code == 401
    assert _sign_in(client, _wrong_code(seed)).status_code == 401
    assert _sign_in(client, pyotp.TOTP(seed).now()).status_code == 200
