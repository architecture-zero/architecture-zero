"""An authenticator setup stages its new key; the live factor stays on.

Until this change POST /api/auth/mfa/setup wrote the NEW seed over the live
one and switched two-factor off in the same update, and POST
/api/auth/mfa/enable switched it back on once a code from the new seed was
confirmed. So a re-key left the account signing in with the password alone
until the confirm - for up to MFA_PENDING_TTL - and a re-key nobody confirmed
left two-factor off for good: the confirm then refused, the old seed was gone,
and nothing switched the factor back on.

The setup now STAGES the new seed - encrypted at rest, in the pending record
the state store already kept, for MFA_PENDING_TTL - and writes nothing to the
account. The confirm checks a code against the staged seed only and, on a
right one, writes the seed and switches two-factor on in one update. Until
then every other door verifies against the live seed, as before.

FIX (each fails on the routes as they were):
  - after a re-key's setup, a sign-in with the password alone is still asked
    for a code, and the current authenticator's code signs in
  - a re-key nobody confirms changes nothing: once its setup has expired the
    account still has two-factor on, with the seed it had
  - until the confirm the account keeps its seed; the confirm with a code of
    the staged seed makes that seed the live one, and the replaced seed's
    codes stop working
  - the confirm with nothing staged is refused before any code is read: the
    same answer for the live seed's right code as for a wrong one, nothing
    counted, nothing checked (no oracle on the live seed)
  - with a re-key staged, the confirm checks the staged seed only: the live
    seed's right code is a wrong code there, and is not spent by it
  - a staged seed that cannot be decrypted (the key rotated since the setup)
    is a 400 that says to start again, never a server error
  - a first enrollment's setup writes nothing to the account either
  - a code of the staged seed opens no door but the confirm: not the
    sign-in, not the re-key's code step-up, not the own-account unlock
  - the staged seed is stored encrypted, never as the key itself
CONTROL (true before and after):
  - a second setup replaces the staged seed: the first one's code is refused
    and the second one's turns it on
  - the admin reset drops a staged seed, and a confirm held across the reset
    turns nothing on
  - no log line carries the staged seed

Throwaway accounts of its own, driven through the app's routes; the three
codes right at any moment (the step before, now, the next) stand in for codes
typed over a minute, as in test_totp_code_used_once.py.
"""
import datetime
import itertools
import threading
import time

import pyotp
import pytest

from app import security, state_store
from app.jwt_auth import MAX_LOGIN_ATTEMPTS, create_access_token, hash_password
from app.users import (create_user, deactivate_user, get_user_by_id,
                       increment_failed_attempts, lock_user, unlock_user)

PW = "StagePassw0rd!x"
WRONG = "Invalid authenticator code"
# This surface's roles: an ordinary account, and the top tier (which may
# reset another account's factor and unlock its own with a code).
MEMBER, TOP = "member", "owner"

_names = itertools.count(1)


def _account(role=MEMBER, mfa=True):
    name = f"stage_{next(_names)}"
    uid = create_user(name, hash_password(PW), role=role)
    seed = None
    if mfa:
        # Enrolled by a direct write of the row as a confirmed setup leaves
        # it (the seed encrypted, the factor on), so the same fixture holds
        # on the routes before this change and after it.
        from app.crypto_at_rest import encrypt_at_rest
        from app.db import get_session
        from app.models import User
        seed = pyotp.random_base32()
        with get_session() as db:
            db.query(User).filter(User.id == uid).update(
                {"mfa_secret": encrypt_at_rest(seed), "mfa_enabled": True})
    token = create_access_token(uid, name, role)
    return {"id": uid, "name": name, "role": role, "seed": seed,
            "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def accounts():
    made = []

    def make(role=MEMBER, mfa=True):
        made.append(_account(role, mfa))
        return made[-1]

    yield make
    for account in made:
        unlock_user(account["id"])
        deactivate_user(account["id"])
        security.clear_mfa_pending(account["id"])


@pytest.fixture(autouse=True)
def _not_at_a_steps_edge():
    """No code made here may change step before it is sent."""
    left = 30 - time.time() % 30
    if left < 2:
        time.sleep(left + 0.05)


def _code(seed, step=0):
    return pyotp.TOTP(seed).at(time.time(), step)


def _wrong_code(seed):
    live = {_code(seed, step) for step in (-1, 0, 1)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in live)


def _row(account):
    return get_user_by_id(account["id"])


def _rekey(client, account, code):
    return client.post("/api/auth/mfa/setup", headers=account["headers"],
                       json={"current_password": PW, "rekey": True, "mfa_code": code})


def _enroll(client, account):
    return client.post("/api/auth/mfa/setup", headers=account["headers"],
                       json={"current_password": PW})


def _confirm(client, account, code):
    return client.post("/api/auth/mfa/enable", headers=account["headers"], json={"code": code})


def _password_step(client, account):
    return client.post("/api/auth/login", json={"username": account["name"], "password": PW})


def _sign_in(client, account, code):
    r = _password_step(client, account)
    assert r.status_code == 200 and r.json().get("mfa_required") is True, \
        f"the password alone was not asked for a code: {r.text}"
    assert "access_token" not in r.json()
    return client.post("/api/auth/mfa/complete",
                       json={"mfa_token": r.json()["mfa_token"], "code": code})


def _codes_counted(account):
    doc = state_store.get(f"totp_fail:{account['id']}")
    return 0 if doc is None else doc["n"]


def _step_recorded(account, seed):
    doc = state_store.get(security._used_code_key(account["id"], seed))
    return None if doc is None else doc["step"]


def _expire_the_setup(account):
    """The pending record as it reads once MFA_PENDING_TTL has passed: the
    row is left as the setup wrote it, with its expiry in the past."""
    from app.db import get_session
    from app.models import SecurityState
    with get_session() as db:
        n = (db.query(SecurityState)
               .filter(SecurityState.key == f"mfa_pending:{account['id']}")
               .update({"expires_at": time.time() - 1}))
    assert n == 1, "the setup left no pending record to expire"


# -- FIX -----------------------------------------------------------------------

def test_a_rekey_in_progress_still_asks_for_a_code_and_the_current_one_signs_in(
        client, accounts):
    someone = accounts()
    old = someone["seed"]
    r = _rekey(client, someone, _code(old, -1))
    assert r.status_code == 200 and r.json()["secret"], r.text
    r = _sign_in(client, someone, _code(old, 0))
    assert r.status_code == 200 and r.json()["access_token"], r.text
    row = _row(someone)
    assert row["mfa_enabled"] is True and row["mfa_secret"] == old


def test_a_rekey_nobody_confirms_leaves_two_factor_on_with_the_seed_it_had(client, accounts):
    someone = accounts()
    old = someone["seed"]
    r = _rekey(client, someone, _code(old, -1))
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    _expire_the_setup(someone)
    r = _confirm(client, someone, _code(new, 0))
    assert r.status_code == 400 and "start" in r.json()["detail"].lower(), r.text
    row = _row(someone)
    assert row["mfa_enabled"] is True and row["mfa_secret"] == old, \
        "an unconfirmed re-key left two-factor off or the seed replaced"
    r = _sign_in(client, someone, _code(old, 0))
    assert r.status_code == 200, r.text


def test_the_confirm_turns_the_staged_seed_live_and_retires_the_old_one(client, accounts):
    someone = accounts()
    old = someone["seed"]
    r = _rekey(client, someone, _code(old, -1))
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    row = _row(someone)
    assert row["mfa_enabled"] is True and row["mfa_secret"] == old, "the setup touched the account"
    r = _confirm(client, someone, _code(new, 0))
    assert r.status_code == 200 and r.json() == {"status": "MFA enabled"}, r.text
    row = _row(someone)
    assert row["mfa_enabled"] is True and row["mfa_secret"] == new
    assert f"mfa_pending:{someone['id']}" not in state_store.keys("mfa_pending:")
    r = _sign_in(client, someone, _code(old, 1))
    assert r.status_code == 401 and r.json() == {"detail": WRONG}, r.text
    r = _sign_in(client, someone, _code(new, 1))
    assert r.status_code == 200 and r.json()["access_token"], r.text


def test_the_confirm_with_nothing_staged_reads_no_code(client, accounts, monkeypatch):
    """An enrolled account, nothing staged. The live seed's right code and a
    wrong code get the same answer, no code is checked against anything and
    none is counted: the confirm is no oracle for the live seed."""
    someone = accounts()
    seed = someone["seed"]
    checked = []
    real = security.totp_code_refusal

    def watched(*args, **kwargs):
        checked.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(security, "totp_code_refusal", watched)
    import app.routers.auth as auth_router
    monkeypatch.setattr(auth_router, "totp_code_refusal", watched)
    answers = []
    for code in (_code(seed, 0), _wrong_code(seed)):
        r = _confirm(client, someone, code)
        answers.append((r.status_code, r.json()))
    assert answers[0] == answers[1], "a right and a wrong code must read alike"
    assert answers[0][0] == 400 and "start" in answers[0][1]["detail"].lower(), answers
    assert checked == [], "a code was checked with nothing staged"
    assert _codes_counted(someone) == 0
    assert _step_recorded(someone, seed) is None, "the live seed's code was spent"
    row = _row(someone)
    assert row["mfa_enabled"] is True and row["mfa_secret"] == seed


def test_with_a_rekey_staged_the_confirm_checks_the_staged_seed_only(client, accounts):
    someone = accounts()
    old = someone["seed"]
    r = _rekey(client, someone, _code(old, -1))
    assert r.status_code == 200, r.text
    r = _confirm(client, someone, _code(old, 1))
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    # Not spent there: the live seed's record still holds the re-key's step.
    assert _step_recorded(someone, old) == pyotp.TOTP(old).timecode(datetime.datetime.now()) - 1
    r = _sign_in(client, someone, _code(old, 1))
    assert r.status_code == 200, r.text


def test_a_staged_seed_that_cannot_be_decrypted_is_a_400_to_start_again(client, accounts):
    from cryptography.fernet import Fernet
    someone = accounts()
    old = someone["seed"]
    r = _rekey(client, someone, _code(old, -1))
    assert r.status_code == 200, r.text
    foreign_seed = pyotp.random_base32()
    foreign = Fernet(Fernet.generate_key()).encrypt(foreign_seed.encode()).decode()
    state_store.put(f"mfa_pending:{someone['id']}", {"since": time.time(), "seed": foreign},
                    security.MFA_PENDING_TTL)
    r = _confirm(client, someone, _code(foreign_seed, 0))
    assert r.status_code == 400 and "start" in r.json()["detail"].lower(), r.text
    row = _row(someone)
    assert row["mfa_enabled"] is True and row["mfa_secret"] == old


def test_a_first_enrollments_setup_writes_nothing_to_the_account(client, accounts):
    someone = accounts(mfa=False)
    r = _enroll(client, someone)
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    row = _row(someone)
    assert row["mfa_secret"] is None and row["mfa_enabled"] is False, \
        "the setup wrote to the account before any code was confirmed"
    r = _confirm(client, someone, _code(new, 0))
    assert r.status_code == 200, r.text
    row = _row(someone)
    assert row["mfa_enabled"] is True and row["mfa_secret"] == new


def test_a_staged_seeds_code_opens_no_door_but_the_confirm(client, accounts):
    top = accounts(TOP)
    old = top["seed"]
    r = _rekey(client, top, _code(old, -1))
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    r = _sign_in(client, top, _code(new, 0))
    assert r.status_code == 401 and r.json() == {"detail": WRONG}, r.text
    r = _rekey(client, top, _code(new, 0))
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    for _ in range(MAX_LOGIN_ATTEMPTS):
        increment_failed_attempts(top["id"])
    lock_user(top["id"], "2099-01-01T00:00:00+00:00")
    r = client.post(f"/api/admin/users/{top['id']}/unlock", headers=top["headers"],
                    json={"mfa_code": _code(new, 0)})
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    unlock_user(top["id"])
    # And the confirm still takes it: none of those spent it.
    r = _confirm(client, top, _code(new, 0))
    assert r.status_code == 200, r.text


def test_the_staged_seed_is_stored_encrypted(client, accounts):
    from app.crypto_at_rest import decrypt_at_rest, is_encrypted
    from app.db import get_session
    from app.models import SecurityState
    someone = accounts()
    r = _rekey(client, someone, _code(someone["seed"], -1))
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    with get_session() as db:
        values = [v for (v,) in db.query(SecurityState.value).all()]
    assert not any(new in v for v in values), "the staged seed is stored in the clear"
    doc = state_store.get(f"mfa_pending:{someone['id']}")
    assert doc and is_encrypted(doc.get("seed")) and decrypt_at_rest(doc["seed"]) == new


# -- CONTROL -------------------------------------------------------------------

def test_control_a_second_setup_replaces_the_staged_seed(client, accounts):
    someone = accounts(mfa=False)
    first = _enroll(client, someone).json()["secret"]
    r = _enroll(client, someone)
    assert r.status_code == 200, r.text
    second = r.json()["secret"]
    assert second != first
    r = _confirm(client, someone, _code(first, 0))
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    r = _confirm(client, someone, _code(second, 0))
    assert r.status_code == 200, r.text
    assert _row(someone)["mfa_secret"] == second


def test_control_the_admin_reset_drops_a_staged_seed(client, accounts):
    top, someone = accounts(TOP), accounts()
    r = _rekey(client, someone, _code(someone["seed"], -1))
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    r = client.post(f"/api/admin/users/{someone['id']}/mfa-reset", headers=top["headers"],
                    json={"current_password": PW})
    assert r.status_code == 200, r.text
    assert f"mfa_pending:{someone['id']}" not in state_store.keys("mfa_pending:")
    r = _confirm(client, someone, _code(new, 0))
    assert r.status_code == 400, r.text
    row = _row(someone)
    assert row["mfa_enabled"] is False and row["mfa_secret"] is None


def test_control_a_confirm_held_across_an_admin_reset_turns_nothing_on(
        client, accounts, monkeypatch):
    top, someone = accounts(TOP), accounts(mfa=False)
    r = _enroll(client, someone)
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    matched, release = threading.Event(), threading.Event()
    real = security._matched_step
    calls = []

    def held(seed, sent):
        step = real(seed, sent)
        calls.append(step)
        if len(calls) == 1:
            matched.set()
            release.wait(20)
        return step

    monkeypatch.setattr(security, "_matched_step", held)
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault(
        "answer", _confirm(client, someone, _code(new, 0))), daemon=True)
    thread.start()
    assert matched.wait(20), "the confirm never matched its code"
    r = client.post(f"/api/admin/users/{someone['id']}/mfa-reset", headers=top["headers"],
                    json={"current_password": PW})
    release.set()
    thread.join(30)
    assert r.status_code == 200, r.text
    confirm = box["answer"]
    assert confirm.status_code == 400 and confirm.json() == {"detail": WRONG}, confirm.text
    row = _row(someone)
    assert row["mfa_enabled"] is False and row["mfa_secret"] is None


def test_control_no_log_line_carries_the_staged_seed(client, accounts, monkeypatch):
    import app.routers.auth as auth_router
    lines = []
    real = auth_router.log
    monkeypatch.setattr(auth_router, "log",
                        lambda *a, **k: (lines.append(repr((a, k))), real(*a, **k)))
    someone = accounts()
    r = _rekey(client, someone, _code(someone["seed"], -1))
    assert r.status_code == 200, r.text
    new = r.json()["secret"]
    r = _confirm(client, someone, _code(new, 0))
    assert r.status_code == 200, r.text
    assert lines, "nothing was logged at all"
    assert not any(new in line for line in lines)
    assert new not in r.text
