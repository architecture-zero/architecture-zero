"""A code that was accepted is not accepted again.

An authenticator code stays right for its own 30 seconds and for the step
either side, and nothing recorded that one had been accepted. So a code
that had just opened one door was good again at another, and at the same
door, until its window ended. A verifier must not accept the second use of a
one-time password (RFC 6238, section 5.2).

Each account now has one record - the newest time step it has had a code
accepted for - and every door that takes a code asks one helper
(security.totp_code_refusal): a code is accepted only if it is right AND its
step is newer than the record, and the record moves in the same write. The
doors on this surface: the sign-in's second step, the code step-up (the
re-key), the unlock of the caller's own account, and the enrollment
confirm.

FIX (each fails on the doors as they were):
  - a code accepted at the sign-in is refused at the re-key and at the
    unlock of the caller's own account, and the sign-in refuses it a second
    time
  - a code accepted at the unlock of the caller's own account is refused at
    the sign-in
  - the code that confirmed a setup is spent: the first sign-in takes the
    next one
  - after a code is accepted, one from an older step is refused, used or not
  - a reused code is a miss: the door's own status, the wrong code's
    sentence plus the clause that a code works once, counted in the code
    bound, against the sign-in's challenge and toward the lock
  - the same right code sent by many requests at once is accepted once
    (every request has matched the code before any of them writes)
  - each seed has its own record and nothing clears it: a sign-in, or a
    re-key, held after matching a code while a re-key with the same code
    runs to the end is refused when it writes (the record per account was
    cleared by that re-key, and the held request was accepted); after an
    operator's reset or a re-key, the new seed's codes meet a fresh record
  - no door checks a code any other way (read from the source)
CONTROL (true before and after, by design):
  - a right fresh code works at each door
  - the next step's code works after a used one, and the step before's
    code works once
  - a wrong code answers and is counted as before at each door
  - a code that cannot be encoded is a wrong code

Every case drives the app's own routes through the suite's test client, on
throwaway accounts of its own. No test waits for a code to change: the three
codes that are right at any moment (the step before, the current one, the
next) stand in for codes typed over a minute.
"""
import ast
import itertools
import threading
import time
from pathlib import Path

import pyotp
import pytest

from app import jwt_auth, security, state_store
from app.jwt_auth import (LOCKOUT_DURATION_MINUTES, MAX_LOGIN_ATTEMPTS,
                          create_access_token, hash_password)
from app.users import (create_user, deactivate_user, enable_mfa, get_user_by_id,
                       increment_failed_attempts, lock_user, set_mfa_secret, unlock_user)

LIMIT = MAX_LOGIN_ATTEMPTS
PW = "OldPassw0rd!x"
WRONG = "Invalid authenticator code"
USED = ("Invalid authenticator code. A code works once: wait for the next "
        "one your authenticator shows.")
UNLOCKED = {"status": "unlocked"}

_names = itertools.count(1)


def _account(role="member", mfa=True):
    """A throwaway account with a known password, a session of its own and,
    unless told otherwise, an authenticator enrolled."""
    name = f"once_{next(_names)}"
    uid = create_user(name, hash_password(PW), role=role)
    seed = None
    if mfa:
        seed = pyotp.random_base32()
        set_mfa_secret(uid, seed)
        enable_mfa(uid)
    token = create_access_token(uid, name, role)
    return {"id": uid, "name": name, "role": role, "seed": seed,
            "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def accounts():
    made = []

    def make(role="member", mfa=True):
        made.append(_account(role, mfa))
        return made[-1]

    yield make
    for account in made:
        unlock_user(account["id"])
        deactivate_user(account["id"])


@pytest.fixture(autouse=True)
def _not_at_a_steps_edge():
    """A test here takes well under a second. Starting one in the last two
    seconds of a 30-second step could let the step change between making a
    code and sending it, so those two seconds are waited out."""
    left = 30 - time.time() % 30
    if left < 2:
        time.sleep(left + 0.05)


def _code(seed, step=0):
    """The code that is right `step` steps from now: -1 the one before, 0
    the current one, 1 the next. All three are accepted at any moment."""
    return pyotp.TOTP(seed).at(time.time(), step)


def _wrong_code(seed):
    live = {_code(seed, step) for step in (-1, 0, 1)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in live)


def _sign_in(client, account, code):
    """The sign-in's two steps: the password, then `code`. Returns the second
    step's response and the challenge it answered."""
    r = client.post("/api/auth/login", json={"username": account["name"], "password": PW})
    assert r.status_code == 200 and r.json().get("mfa_required"), r.text
    token = r.json()["mfa_token"]
    return client.post("/api/auth/mfa/complete", json={"mfa_token": token, "code": code}), token


def _rekey(client, account, code):
    return client.post("/api/auth/mfa/setup", headers=account["headers"],
                       json={"current_password": PW, "rekey": True, "mfa_code": code})


def _unlock_own(client, account, code):
    return client.post(f"/api/admin/users/{account['id']}/unlock", headers=account["headers"],
                       json={"mfa_code": code})


def _lock(account):
    for _ in range(LIMIT):
        increment_failed_attempts(account["id"])
    lock_user(account["id"], "2099-01-01T00:00:00+00:00")


def _row(account):
    return get_user_by_id(account["id"])


def _codes_counted(account):
    doc = state_store.get(f"totp_fail:{account['id']}")
    return 0 if doc is None else doc["n"]


def _step_recorded(account, seed=None):
    """The newest step recorded as accepted for the account's seed (or the
    one given): the record is kept per seed. The key is asked for the way the
    helper as it was took its arguments too (one record per account), so a
    case here fails there for what the code did, not for how it was called."""
    import inspect
    if "seed" in inspect.signature(security._used_code_key).parameters:
        key = security._used_code_key(account["id"], seed or account["seed"])
    else:
        key = security._used_code_key(account["id"])
    doc = state_store.get(key)
    return None if doc is None else doc["step"]


def _challenge_misses(token):
    _, jti = jwt_auth.decode_mfa_challenge_token(token)
    doc = state_store.get(f"mfa:{jti}")
    return 0 if doc is None else doc["attempts"]


# -- FIX: a code accepted at one door is refused at the others ------------------

def test_a_code_accepted_at_the_sign_in_is_refused_at_the_rekey(client, accounts):
    """The sign-in's code, sent again as the re-key's proof: a 400 with the
    wrong code's sentence and the clause that a code works once, counted in
    the code bound and toward the lock like any wrong code. The seed is not
    replaced."""
    someone = accounts()
    code = _code(someone["seed"])
    r, _ = _sign_in(client, someone, code)
    assert r.status_code == 200 and r.json()["access_token"], r.text
    r = _rekey(client, someone, code)
    assert r.status_code == 400 and r.json() == {"detail": USED}, r.text
    assert _codes_counted(someone) == 1, "a reused code was not counted in the code bound"
    row = _row(someone)
    assert row["failed_attempts"] == 1, "a reused code did not count toward the lock"
    assert row["mfa_enabled"] and row["mfa_secret"] == someone["seed"]
    r = _rekey(client, someone, _code(someone["seed"], 1))
    assert r.status_code == 200 and r.json()["secret"], r.text


def test_a_code_accepted_at_the_sign_in_is_refused_at_the_own_account_unlock(client, accounts):
    """The same at the unlock of the caller's own account: refused, counted,
    the lock left exactly as it stands, and the window's one unlock handed
    back - so the next code still unlocks."""
    top = accounts("owner")
    code = _code(top["seed"])
    r, _ = _sign_in(client, top, code)
    assert r.status_code == 200, r.text
    _lock(top)
    locked = _row(top)
    r = _unlock_own(client, top, code)
    assert r.status_code == 400 and r.json() == {"detail": USED}, r.text
    assert _codes_counted(top) == 1
    after = _row(top)
    assert (after["locked_until"], after["failed_attempts"]) == \
        (locked["locked_until"], locked["failed_attempts"]), "a reused code moved the lock"
    r = _unlock_own(client, top, _code(top["seed"], 1))
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text


def test_the_sign_in_refuses_the_same_code_a_second_time(client, accounts):
    """The same door, twice. The second use is the sign-in's own 401, counted
    against that sign-in's challenge and toward the account's lock as a
    wrong code is; the next code then signs in."""
    someone = accounts()
    code = _code(someone["seed"])
    r, _ = _sign_in(client, someone, code)
    assert r.status_code == 200, r.text
    r, challenge = _sign_in(client, someone, code)
    assert r.status_code == 401 and r.json() == {"detail": USED}, r.text
    assert _challenge_misses(challenge) == 1, "a reused code was not counted against the challenge"
    assert _codes_counted(someone) == 1 and _row(someone)["failed_attempts"] == 1
    r, _ = _sign_in(client, someone, _code(someone["seed"], 1))
    assert r.status_code == 200 and r.json()["access_token"], r.text


def test_a_code_accepted_at_the_own_account_unlock_is_refused_at_the_sign_in(client, accounts):
    top = accounts("owner")
    _lock(top)
    code = _code(top["seed"])
    r = _unlock_own(client, top, code)
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    r, _ = _sign_in(client, top, code)
    assert r.status_code == 401 and r.json() == {"detail": USED}, r.text
    r, _ = _sign_in(client, top, _code(top["seed"], 1))
    assert r.status_code == 200, r.text


def test_the_code_that_confirmed_a_setup_is_spent(client, accounts):
    """Enrolling: the setup, then the confirm with the new authenticator's
    code. That code has been accepted, so the first sign-in takes the next
    one."""
    someone = accounts(mfa=False)
    r = client.post("/api/auth/mfa/setup", headers=someone["headers"],
                    json={"current_password": PW})
    assert r.status_code == 200, r.text
    someone["seed"] = r.json()["secret"]
    code = _code(someone["seed"])
    r = client.post("/api/auth/mfa/enable", headers=someone["headers"], json={"code": code})
    assert r.status_code == 200 and r.json() == {"status": "MFA enabled"}, r.text
    r, _ = _sign_in(client, someone, code)
    assert r.status_code == 401 and r.json() == {"detail": USED}, r.text
    r, _ = _sign_in(client, someone, _code(someone["seed"], 1))
    assert r.status_code == 200, r.text


def test_after_a_code_is_accepted_an_older_one_is_refused(client, accounts):
    """The record is the newest step accepted, so once the next step's code
    has been accepted the current step's is refused - used or not. The
    record does not move back."""
    someone = accounts()
    r, _ = _sign_in(client, someone, _code(someone["seed"], 1))
    assert r.status_code == 200, r.text
    newest = _step_recorded(someone)
    r, _ = _sign_in(client, someone, _code(someone["seed"], 0))
    assert r.status_code == 401 and r.json() == {"detail": USED}, r.text
    r, _ = _sign_in(client, someone, _code(someone["seed"], -1))
    assert r.status_code == 401 and r.json() == {"detail": USED}, r.text
    assert _step_recorded(someone) == newest


def test_reused_codes_stop_at_the_code_bound_as_wrong_codes_do(client, accounts, monkeypatch):
    """A reused code at the re-key, again and again. Each one is counted in
    the code bound - the count a right password does not clear - and past
    the cap the door answers 429 before it reads a code, as it does for
    wrong ones."""
    monkeypatch.setattr(security, "TOTP_MAX_FAILURES", 3)
    someone = accounts()
    code = _code(someone["seed"])
    r, _ = _sign_in(client, someone, code)
    assert r.status_code == 200, r.text
    for n in (1, 2, 3):
        r = _rekey(client, someone, code)
        assert r.status_code == 400 and r.json() == {"detail": USED}, (n, r.text)
        assert _codes_counted(someone) == n
    r = _rekey(client, someone, code)
    assert r.status_code == 429, r.text
    assert r.json()["detail"].startswith("Too many wrong authenticator codes on this account.")
    assert _row(someone)["mfa_secret"] == someone["seed"]


def test_reused_codes_at_the_sign_in_lock_the_account_as_wrong_codes_do(client, accounts,
                                                                       monkeypatch):
    """At the sign-in nothing clears the lock's count between tries, so
    reused codes walk into the lock: the one that reaches the limit answers
    429 and the lock is stored."""
    monkeypatch.setattr(security, "TOTP_MAX_FAILURES", 4 * LIMIT)
    someone = accounts()
    code = _code(someone["seed"])
    r, _ = _sign_in(client, someone, code)
    assert r.status_code == 200, r.text
    for n in range(1, LIMIT):
        r, _ = _sign_in(client, someone, code)
        assert r.status_code == 401 and r.json() == {"detail": USED}, (n, r.text)
        assert _row(someone)["failed_attempts"] == n
    r, _ = _sign_in(client, someone, code)
    assert r.status_code == 429, r.text
    assert r.json() == {"detail": f"Too many failed attempts. Account locked for "
                                  f"{LOCKOUT_DURATION_MINUTES} minutes."}
    assert _row(someone)["locked_until"]


# -- FIX: the same right code, sent by many requests at once --------------------

BURST = 8


def test_the_same_right_code_sent_at_once_is_accepted_once(accounts, monkeypatch):
    """Eight callers hand the helper the same right code. Each is held after
    its code has been matched and before anything is written, until all
    eight are there - so every one of them has seen a right code and an
    unmoved record. Then they write. One is accepted; seven are told the
    code was used. Nothing here is timed: the count is the proof."""
    someone = accounts()
    code = _code(someone["seed"])
    real = security._matched_step
    matched = threading.Barrier(BURST, timeout=20)

    def held(seed, sent):
        step = real(seed, sent)
        matched.wait()
        return step

    monkeypatch.setattr(security, "_matched_step", held)
    answers = []

    def ask():
        answers.append(security.totp_code_refusal(someone["id"], someone["seed"], code))

    threads = [threading.Thread(target=ask, daemon=True) for _ in range(BURST)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(answers) == BURST, "a caller did not come back"
    assert answers.count(None) == 1, f"{answers.count(None)} callers had the same code accepted"
    assert answers.count(USED) == BURST - 1, answers


# -- FIX: one record per seed, and nothing clears it ---------------------------
# A record per account had to be cleared when the seed changed, and a request
# that had matched a code against the seed it read at its start then found
# no record and was accepted: the same code twice. These two hold such a
# request after its match, run the re-key with the same code to the end, and
# only then let the held request write. Nothing is timed: the order is forced.

def _hold_the_first_match(monkeypatch):
    """The first code matched while this is installed is held - after it has
    matched, before anything is written - until `release` is set."""
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
    return matched, release


def _in_the_background(send):
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("answer", send()), daemon=True)
    thread.start()
    return thread, box


def test_a_sign_in_held_across_a_rekey_with_the_same_code_is_refused(client, accounts,
                                                                    monkeypatch):
    """A sign-in matches code C against the seed it read, and is held. The
    re-key, with the same C, is accepted and stores a new seed. The held
    sign-in then writes: it meets its own seed's record and is refused as a
    reuse - it used to find the record cleared and sign in."""
    someone = accounts()
    code = _code(someone["seed"])
    r = client.post("/api/auth/login", json={"username": someone["name"], "password": PW})
    assert r.status_code == 200 and r.json().get("mfa_required"), r.text
    token = r.json()["mfa_token"]
    matched, release = _hold_the_first_match(monkeypatch)
    held, box = _in_the_background(lambda: client.post(
        "/api/auth/mfa/complete", json={"mfa_token": token, "code": code}))
    assert matched.wait(20), "the sign-in never matched its code"
    rekey = _rekey(client, someone, code)
    release.set()
    held.join(30)
    assert rekey.status_code == 200 and rekey.json().get("secret"), rekey.text
    sign_in = box["answer"]
    assert sign_in.status_code == 401 and sign_in.json() == {"detail": USED}, \
        f"one code accepted twice: re-key 200, sign-in {sign_in.status_code} {sign_in.text}"


def test_a_rekey_held_across_a_rekey_with_the_same_code_is_refused(client, accounts,
                                                                  monkeypatch):
    """Two re-keys with the same code, the first held after its match while
    the second runs to the end. The held one is refused as a reuse; it used
    to be accepted too, and to store a seed of its own."""
    someone = accounts()
    code = _code(someone["seed"])
    matched, release = _hold_the_first_match(monkeypatch)
    held, box = _in_the_background(lambda: _rekey(client, someone, code))
    assert matched.wait(20), "the first re-key never matched its code"
    second = _rekey(client, someone, code)
    release.set()
    held.join(30)
    assert second.status_code == 200 and second.json().get("secret"), second.text
    first = box["answer"]
    assert first.status_code == 400 and first.json() == {"detail": USED}, \
        f"one code accepted twice: {second.status_code}, {first.status_code} {first.text}"


def _record_deletes(monkeypatch):
    """Every key under totp_used: the store is asked to delete."""
    deleted = []
    real = state_store.delete

    def delete(key):
        if key.startswith("totp_used:"):
            deleted.append(key)
        return real(key)

    monkeypatch.setattr(state_store, "delete", delete)
    return deleted


def test_after_an_operators_reset_a_new_seed_meets_a_fresh_record(client, accounts,
                                                                 monkeypatch):
    """The account signs in with the next step's code, an operator resets its
    factor, and it enrolls again. The new seed's code for that very step is
    accepted: its record is its own. Nothing was cleared, and the old seed's
    record is still there until it expires."""
    deleted = _record_deletes(monkeypatch)
    top, someone = accounts("owner"), accounts()
    old_seed = someone["seed"]
    r, _ = _sign_in(client, someone, _code(old_seed, 1))
    assert r.status_code == 200, r.text
    old_step = _step_recorded(someone, old_seed)
    assert old_step is not None, "an accepted code left no record"
    r = client.post(f"/api/admin/users/{someone['id']}/mfa-reset", headers=top["headers"],
                    json={"current_password": PW})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/mfa/setup", headers=someone["headers"],
                    json={"current_password": PW})
    assert r.status_code == 200, r.text
    new_seed = r.json()["secret"]
    r = client.post("/api/auth/mfa/enable", headers=someone["headers"],
                    json={"code": _code(new_seed, 1)})
    assert r.status_code == 200 and r.json() == {"status": "MFA enabled"}, r.text
    assert _step_recorded(someone, new_seed) == old_step
    assert _step_recorded(someone, old_seed) == old_step, "the old seed's record was cleared"
    assert deleted == [], deleted


def test_a_replacements_new_seed_meets_a_fresh_record_and_nothing_is_cleared(client, accounts,
                                                                           monkeypatch):
    """The re-key's proof is the next step's code from the authenticator being
    replaced, and it is recorded against that seed. The new seed's first code
    is for the CURRENT step - older than the one just accepted - and is
    accepted: it is judged against the new seed's own record. Nothing was
    cleared on the way."""
    deleted = _record_deletes(monkeypatch)
    someone = accounts()
    old_seed = someone["seed"]
    r = _rekey(client, someone, _code(old_seed, 1))
    assert r.status_code == 200, r.text
    new_seed = r.json()["secret"]
    old_step = _step_recorded(someone, old_seed)
    assert old_step is not None, "the re-key's code was not recorded"
    r = client.post("/api/auth/mfa/enable", headers=someone["headers"],
                    json={"code": _code(new_seed, 0)})
    assert r.status_code == 200 and r.json() == {"status": "MFA enabled"}, r.text
    assert _step_recorded(someone, new_seed) == old_step - 1
    assert _step_recorded(someone, old_seed) == old_step
    assert deleted == [], deleted


# -- FIX: no door checks a code any other way ----------------------------------

# Every function under app/ that takes an authenticator code from a caller.
_DOORS = {
    ("jwt_auth.py", "_check_current_code"),      # the code step-up and the own-account unlock
    ("routers/auth.py", "mfa_complete"),         # the sign-in's second step
    ("routers/auth.py", "mfa_enable"),           # the enrollment confirm
}


def test_every_door_asks_the_one_helper_and_none_verifies_a_code_itself():
    """Read from the source. No function under app/ calls a .verify() - the
    one-time-password library's own check, which accepts a right code as
    often as it is sent - and the functions that ask the helper are exactly
    the doors named above. A new door that checks a code itself, or one that
    is not named here, fails."""
    app_dir = Path(jwt_auth.__file__).resolve().parent
    verifies, asks = [], set()
    for path in sorted(app_dir.rglob("*.py")):
        rel = path.relative_to(app_dir).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Attribute) and call.func.attr == "verify":
                    verifies.append((rel, node.name))
                if getattr(call.func, "id", "") == "totp_code_refusal":
                    asks.add((rel, node.name))
    assert verifies == [], verifies
    assert asks == _DOORS, asks ^ _DOORS


# -- CONTROL -------------------------------------------------------------------

def test_control_a_right_fresh_code_works_at_each_door(client, accounts):
    someone, top, new = accounts(), accounts("owner"), accounts(mfa=False)
    r, _ = _sign_in(client, someone, _code(someone["seed"]))
    assert r.status_code == 200 and r.json()["access_token"], r.text
    r = _rekey(client, top, _code(top["seed"]))
    assert r.status_code == 200 and r.json()["secret"], r.text
    locked = accounts("admin")
    _lock(locked)
    r = _unlock_own(client, locked, _code(locked["seed"]))
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    r = client.post("/api/auth/mfa/setup", headers=new["headers"], json={"current_password": PW})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/mfa/enable", headers=new["headers"],
                    json={"code": _code(r.json()["secret"])})
    assert r.status_code == 200 and r.json() == {"status": "MFA enabled"}, r.text


def test_control_the_next_steps_code_works_after_a_used_one(client, accounts):
    """Three codes are right at any moment. Used oldest first, each is newer
    than the last one accepted, so all three work - the step before's code
    is still good once, as it always was."""
    someone = accounts()
    for step in (-1, 0):
        r, _ = _sign_in(client, someone, _code(someone["seed"], step))
        assert r.status_code == 200, (step, r.text)
    r = _rekey(client, someone, _code(someone["seed"], 1))
    assert r.status_code == 200, r.text


def test_control_a_wrong_code_answers_and_is_counted_as_before(client, accounts):
    """The sentence and the status at each door, the code bound's count, the
    challenge's count and the lock's count: all as they were."""
    someone, top = accounts(), accounts("owner")
    wrong = _wrong_code(someone["seed"])
    r, challenge = _sign_in(client, someone, wrong)
    assert r.status_code == 401 and r.json() == {"detail": WRONG}, r.text
    assert _challenge_misses(challenge) == 1
    r = _rekey(client, someone, wrong)
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    # Two in the code bound; one toward the lock, because the re-key's right
    # password cleared the sign-in's miss before the code was read.
    assert _codes_counted(someone) == 2 and _row(someone)["failed_attempts"] == 1
    _lock(top)
    r = _unlock_own(client, top, _wrong_code(top["seed"]))
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    assert _codes_counted(top) == 1
    new = accounts(mfa=False)
    r = client.post("/api/auth/mfa/setup", headers=new["headers"], json={"current_password": PW})
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/mfa/enable", headers=new["headers"],
                    json={"code": _wrong_code(r.json()["secret"])})
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    assert _codes_counted(new) == 1 and not _row(new)["mfa_enabled"]


def test_control_a_code_that_cannot_be_encoded_is_a_wrong_code(client, accounts):
    import json
    someone = accounts()
    r = client.post("/api/auth/mfa/setup", headers={**someone["headers"],
                                                    "Content-Type": "application/json"},
                    content=json.dumps({"current_password": PW, "rekey": True,
                                        "mfa_code": "12" + chr(0xD800) + "456"}))
    assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text
    assert _codes_counted(someone) == 1
