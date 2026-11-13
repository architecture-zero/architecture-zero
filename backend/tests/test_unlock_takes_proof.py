"""An unlock takes proof.

POST /api/admin/users/{id}/unlock clears the sign-in's lock and its count of
misses. That lock is what bounds password guesses at the sign-in for one
account, so what this file pins is what clearing it costs:

  - your OWN account: a current authenticator code, counted in the
    per-account code bound before it is read, like every other code. The lock
    does not refuse this door (the lock is what a right code clears), and a
    wrong code while the lock stands leaves the lock exactly as it is. An
    account with no authenticator enrolled cannot unlock itself: it is told
    to wait, with the minutes, or whom to ask;
  - SOMEONE ELSE'S account: the caller's own password, through the one
    step-up helper every other door to another account's durable state uses.
    A missing or wrong one is a string 400, a wrong one is counted, and a
    caller whose own account is locked is told whose lock it is;
  - the ceiling is unchanged: only an Owner unlocks an Owner's account;
  - a session alone unlocks nothing.

Two things in the same files:

  - a password that cannot be encoded (JSON can carry half of a surrogate
    pair) is a wrong password at the sign-in: the same answer, counted as
    one miss. It used to be a server error;
  - a NEW password that cannot be encoded is refused in words by every route
    that sets one. It used to be a server error too.

FIX (each fails on the route and the helpers as they were):
  - wrong sign-ins with an unlock of the caller's own account attempted
    between rounds, and no code: the account locks at the limit and stays
    locked
  - the same for an account with an authenticator, sending no code, a
    password in place of the code, or an empty body
  - unlocking your own account with a wrong code is refused and counted,
    and the lock is left as it stands; a right code unlocks, and the
    sign-in then works
  - codes at this door stop at the code bound, a right one included
  - with no lock standing, a wrong code counts toward the lock
  - an account with no authenticator is told to wait, with the minutes,
    or whom to ask
  - unlocking another account without the caller's password, or with a
    wrong one, is refused, and a wrong one is counted; the right one
    unlocks
  - wrong passwords at this door lock the caller, not the target, and a
    locked caller is told whose lock it is
  - a locked caller with an authenticator unlocks itself first, then the
    other account
  - a sign-in password that cannot be encoded is a wrong password
  - a new password that cannot be encoded is refused in words
  - an own-account unlock is allowed once per lockout window: the second
    is refused, a right code included, before the code is read; the loop
    of unlock-and-guess stops; a wrong code does not use the window's one
    unlock up; after the window it works again; an administrator's unlock
    of the same account is neither counted nor a way to clear the count
  - a code that cannot be encoded is a wrong code at every door that takes
    one: the own-account unlock, the sign-in's second step, the enrollment
    confirm
  - an id that names no account is a 404, after the proof; the ceiling
    holds for a deactivated account
  - a username that cannot be encoded is an unknown username at the
    sign-in, at the cost of one, and is refused in words where an account
    is created or renamed
CONTROL (true before and after, by design):
  - an administrator with the right password unlocks another account, and
    that person signs in at once
  - the ceiling answers as it did
  - the sign-in's answers and its lock are unchanged for an ordinary wrong
    password
  - an account without manage_users is refused as before
  - every function that hashes a new password asks the policy about it
    first (read from the source)
  - every function that clears a lock or its count of misses is on a short
    list here, with the reason it may (read from the source)
  - every function that creates or renames an account asks about the name
    first (read from the source)

Every case drives the app's own routes through the suite's test client, on
throwaway accounts of its own.
"""
import ast
import itertools
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyotp
import pytest

from app import jwt_auth, security, state_store
from app.jwt_auth import (LOCKOUT_DURATION_MINUTES, MAX_LOGIN_ATTEMPTS,
                          create_access_token, hash_password, validate_password)
from app.users import (create_user, deactivate_user, enable_mfa, get_user_by_id,
                       get_user_by_username, lock_user, set_mfa_secret, unlock_user)

LIMIT = MAX_LOGIN_ATTEMPTS
PW = "OldPassw0rd!x"
NEW_PW = "NewPassw0rd!y"
NOT_IT = "not-the-password"
CANNOT_BE_ENCODED = "not" + chr(0xD800) + "it"
# Long enough, and mixed enough, to pass every other rule of the policy.
NEW_PW_CANNOT_BE_ENCODED = NEW_PW + chr(0xD800)
SIGN_IN_REFUSED = {"detail": "Invalid username or password"}
UNLOCKED = {"status": "unlocked"}
WRONG_PW = {"detail": "Current password is incorrect"}
PW_REQUIRED = {"detail": "Your current password is required to unlock an account"}
WRONG_CODE = {"detail": "Invalid authenticator code"}
CODE_REQUIRED = {"detail": "A current authenticator code is required to unlock your own account"}
LOCKED_NOW = {"detail": f"Too many failed attempts. Account locked for "
                        f"{LOCKOUT_DURATION_MINUTES} minutes."}
NOT_STORABLE = {"detail": "Password contains a character that cannot be stored. "
                          "Choose a different password"}
NO_AUTHENTICATOR = ("Unlocking your own account takes a current authenticator code, "
                    "and your account has no authenticator enrolled. ")

_names = itertools.count(1)


def _account(role, mfa=False):
    """A throwaway account with a known password and a session of its own.
    The session's token is minted here, so no test spends the sign-in's
    per-address budget on getting one. With `mfa`, an authenticator is
    enrolled and its seed is returned with the account."""
    name = f"utp_{next(_names)}"
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
    """Makes throwaway accounts by role and retires each when the test ends,
    so no test here leaves an extra Owner or a lock behind for the next."""
    made = []

    def make(role="member", mfa=False):
        made.append(_account(role, mfa))
        return made[-1]

    yield make
    for account in made:
        unlock_user(account["id"])
        deactivate_user(account["id"])


def _unlock(client, caller, target, **body):
    """The unlock route as `caller`, for `target`. With no body named the
    request carries none at all - what a client written for the route as it
    was sends."""
    path = f"/api/admin/users/{target['id']}/unlock"
    if not body:
        return client.post(path, headers=caller["headers"])
    return client.post(path, headers=caller["headers"], json=body)


def _sign_in(client, account, password):
    return client.post("/api/auth/login",
                       json={"username": account["name"], "password": password})


def _row(account):
    return get_user_by_id(account["id"])


def _in_30_minutes():
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


def _lock(account):
    """The account as LIMIT wrong sign-ins leave it: the count at the limit
    and a lock stored."""
    from app.users import increment_failed_attempts
    for _ in range(LIMIT):
        increment_failed_attempts(account["id"])
    lock_user(account["id"], _in_30_minutes())


def _wrong_code(seed):
    """A code none of the three accepted steps (now, and one either side)
    matches, so no test here can pass or fail by accident."""
    totp = pyotp.TOTP(seed)
    live = {totp.at(time.time() + step) for step in (-30, 0, 30)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in live)


def _codes_counted(account):
    doc = state_store.get(f"totp_fail:{account['id']}")
    return 0 if doc is None else doc["n"]


def _passwords_counted(account):
    doc = state_store.get(f"pw_fail:{account['id']}")
    return 0 if doc is None else doc["n"]


def _own_unlocks_counted(account):
    doc = state_store.get(f"own_unlock:{account['id']}")
    return 0 if doc is None else doc["n"]


def _lock_of(account):
    """The lock stored on the account's row whatever its status - _row reads
    through get_user_by_id, which hides a deactivated account."""
    from app.db import get_session
    from app.models import User
    with get_session() as db:
        return db.query(User.locked_until).filter(User.id == account["id"]).one()[0]


class _HashRounds:
    """Counts the bcrypt rounds paid while it is installed: every check goes
    through bcrypt.checkpw, whichever function asked for it."""

    def __init__(self, monkeypatch):
        self.paid = 0
        real = jwt_auth.bcrypt.checkpw

        def checkpw(password, hashed):
            self.paid += 1
            return real(password, hashed)

        monkeypatch.setattr(jwt_auth.bcrypt, "checkpw", checkpw)


_WAIT_OR_ASK = re.compile(r"Wait (\d+) minute\(s\) for the lock to end, or ask (.+) to unlock it\.")
_OWN_LOCK = re.compile(r"Your own account is locked, so your password is not accepted to "
                       r"unlock another account\. Try again in (\d+) minute\(s\)\.(.*)")


def _told_to_wait_or_ask(response, who_else):
    """The refusal for an account with no authenticator, while its lock
    stands: 400, the reason, a whole number of minutes no longer than the
    lock, and whom to ask."""
    detail = response.json().get("detail")
    if response.status_code != 400 or not isinstance(detail, str):
        return False
    if not detail.startswith(NO_AUTHENTICATOR):
        return False
    said = _WAIT_OR_ASK.fullmatch(detail[len(NO_AUTHENTICATOR):])
    return (said is not None and 1 <= int(said.group(1)) <= 30
            and said.group(2) == who_else)


# -- FIX: a session alone does not unlock its own account ---------------------

def test_wrong_sign_ins_with_a_self_unlock_between_rounds_lock_the_account(client, accounts):
    """Wrong sign-ins for one account, with an unlock of that account from
    its own session attempted before the limit and again after it, and no
    code. The unlock is refused both times, so the count is not cleared: the
    account locks at the limit and stays locked - the right password
    included."""
    admin = accounts("admin")
    for _ in range(LIMIT - 1):
        r = _sign_in(client, admin, NOT_IT)
        assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text
    r = _unlock(client, admin, admin)
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": NO_AUTHENTICATOR + "Your account is not locked now."}
    assert _row(admin)["failed_attempts"] == LIMIT - 1, "a refused unlock cleared the count"
    r = _sign_in(client, admin, NOT_IT)
    assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text
    assert _row(admin)["locked_until"], "the limit was reached and no lock landed"
    r = _unlock(client, admin, admin)
    assert _told_to_wait_or_ask(r, "another administrator"), (r.status_code, r.text)
    assert _row(admin)["locked_until"], "a refused unlock cleared the lock"
    r = _sign_in(client, admin, PW)
    assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text


@pytest.mark.parametrize("body", [{}, {"mfa_code": ""}, {"current_password": PW},
                                  {"current_password": PW, "mfa_code": "  "}],
                         ids=["no body", "an empty code", "the password instead",
                              "the password and a blank code"])
def test_an_enrolled_account_does_not_unlock_itself_without_a_code(client, accounts,
                                                                    monkeypatch, body):
    """With an authenticator enrolled, the caller's own account takes the
    code and nothing else: no body, an empty code and the account's own
    password are each a string 400, nothing is counted or hashed, and the
    lock stays."""
    owner = accounts("owner", mfa=True)
    _lock(owner)
    hashed = []
    real = jwt_auth.verify_password
    monkeypatch.setattr(jwt_auth, "verify_password",
                        lambda plain, stored: hashed.append(plain) or real(plain, stored))
    r = _unlock(client, owner, owner, **body)
    assert r.status_code == 400 and r.json() == CODE_REQUIRED, r.text
    assert hashed == [], "a password was checked at the code door"
    assert _codes_counted(owner) == 0 and _passwords_counted(owner) == 0
    row = _row(owner)
    assert row["locked_until"] and row["failed_attempts"] == LIMIT


# -- FIX: your own account takes a current code -------------------------------

def test_a_self_unlock_with_a_wrong_code_is_refused_and_a_right_one_unlocks(client, accounts):
    """A wrong code is refused and counted in the code bound, and the lock is
    left exactly as it stands - not cleared, not lengthened. A right code
    unlocks: the lock and its count are gone, the code count is cleared, and
    the sign-in works at once."""
    owner = accounts("owner", mfa=True)
    for _ in range(LIMIT):
        assert _sign_in(client, owner, NOT_IT).status_code == 401
    locked = _row(owner)
    assert locked["locked_until"] and locked["failed_attempts"] == LIMIT

    r = _unlock(client, owner, owner, mfa_code=_wrong_code(owner["seed"]))
    assert r.status_code == 400 and r.json() == WRONG_CODE, r.text
    assert _codes_counted(owner) == 1, "the wrong code was not counted"
    after = _row(owner)
    assert (after["locked_until"], after["failed_attempts"]) == \
        (locked["locked_until"], locked["failed_attempts"]), "a wrong code moved the lock"

    r = _unlock(client, owner, owner, mfa_code=pyotp.TOTP(owner["seed"]).now())
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    row = _row(owner)
    assert not row["locked_until"] and row["failed_attempts"] == 0
    assert _codes_counted(owner) == 0, "a right code did not clear the code count"

    r = _sign_in(client, owner, PW)
    assert r.status_code == 200 and r.json().get("mfa_required"), r.text
    r = client.post("/api/auth/mfa/complete",
                    json={"mfa_token": r.json()["mfa_token"],
                          "code": pyotp.TOTP(owner["seed"]).now()})
    assert r.status_code == 200 and "access_token" in r.json(), r.text


def test_codes_at_the_self_unlock_stop_at_the_code_bound(client, accounts, monkeypatch):
    """Every code at this door is counted before it is read, in the one
    count every code door shares: past the cap a RIGHT code is refused too,
    and the account stays locked."""
    monkeypatch.setattr(security, "TOTP_MAX_FAILURES", 3)
    owner = accounts("owner", mfa=True)
    _lock(owner)
    for _ in range(3):
        r = _unlock(client, owner, owner, mfa_code=_wrong_code(owner["seed"]))
        assert r.status_code == 400 and r.json() == WRONG_CODE, r.text
    r = _unlock(client, owner, owner, mfa_code=pyotp.TOTP(owner["seed"]).now())
    assert r.status_code == 429, r.text
    assert r.json()["detail"].startswith("Too many wrong authenticator codes on this account."), r.text
    assert _row(owner)["locked_until"], "a code past the cap unlocked the account"


def test_a_wrong_code_with_no_lock_standing_counts_toward_the_lock(client, accounts):
    """With no lock standing, a wrong code at this door is a step-up miss
    like any other: it counts toward the lock, and the one that reaches the
    limit locks the account."""
    admin = accounts("admin", mfa=True)
    for n in range(1, LIMIT):
        r = _unlock(client, admin, admin, mfa_code=_wrong_code(admin["seed"]))
        assert r.status_code == 400 and r.json() == WRONG_CODE, r.text
        assert _row(admin)["failed_attempts"] == n
    r = _unlock(client, admin, admin, mfa_code=_wrong_code(admin["seed"]))
    assert r.status_code == 429 and r.json() == LOCKED_NOW, r.text
    assert _row(admin)["locked_until"]


@pytest.mark.parametrize("role, who_else", [("owner", "another Owner"),
                                            ("admin", "another administrator")])
def test_an_account_with_no_authenticator_is_told_to_wait_or_whom_to_ask(client, accounts,
                                                                         role, who_else):
    """No authenticator, no proof to give: the account cannot unlock itself,
    whatever it sends. The refusal says how long the lock has left and who
    else may unlock an account of this tier."""
    account = accounts(role)
    _lock(account)
    for body in ({}, {"current_password": PW}, {"mfa_code": "123456"}):
        r = _unlock(client, account, account, **body)
        assert _told_to_wait_or_ask(r, who_else), (body, r.status_code, r.text)
    assert _codes_counted(account) == 0 and _passwords_counted(account) == 0
    row = _row(account)
    assert row["locked_until"] and row["failed_attempts"] == LIMIT
    r = _sign_in(client, account, PW)
    assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text


# -- FIX: someone else's account takes the caller's own password --------------

def test_unlocking_another_account_takes_the_callers_password(client, accounts):
    """Without the caller's password, or with a wrong one, nothing is
    unlocked; a wrong one is counted against the caller, in the lock's count
    and in the count of passwords checked. The right one unlocks."""
    admin, member = accounts("admin"), accounts("member")
    _lock(member)
    for body in ({}, {"current_password": ""}, {"mfa_code": "123456"}):
        r = _unlock(client, admin, member, **body)
        assert r.status_code == 400 and r.json() == PW_REQUIRED, (body, r.text)
    assert _row(admin)["failed_attempts"] == 0 and _passwords_counted(admin) == 0
    assert _row(member)["locked_until"], "an unlock with no password cleared the lock"

    r = _unlock(client, admin, member, current_password=NOT_IT)
    assert r.status_code == 400 and r.json() == WRONG_PW, r.text
    assert _row(admin)["failed_attempts"] == 1 and _passwords_counted(admin) == 1
    assert _row(member)["locked_until"], "an unlock with a wrong password cleared the lock"

    r = _unlock(client, admin, member, current_password=PW)
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert not _row(member)["locked_until"] and _row(member)["failed_attempts"] == 0
    assert _row(admin)["failed_attempts"] == 0 and _passwords_counted(admin) == 0
    assert _sign_in(client, member, PW).status_code == 200


def test_wrong_passwords_at_the_unlock_lock_the_caller_and_not_the_target(client, accounts):
    """Wrong passwords at this door count against the CALLER, as at every
    step-up door: the one that reaches the limit locks the caller. After
    that the right password is refused too, in words that say whose lock it
    is, and the other account stays locked throughout."""
    admin, member = accounts("admin"), accounts("member")
    _lock(member)
    for _ in range(LIMIT - 1):
        r = _unlock(client, admin, member, current_password=NOT_IT)
        assert r.status_code == 400 and r.json() == WRONG_PW, r.text
    r = _unlock(client, admin, member, current_password=NOT_IT)
    assert r.status_code == 429 and r.json() == LOCKED_NOW, r.text
    assert _row(admin)["locked_until"]
    r = _unlock(client, admin, member, current_password=PW)
    said = _OWN_LOCK.fullmatch(r.json().get("detail") or "")
    assert r.status_code == 429 and said, r.text
    assert 1 <= int(said.group(1)) <= LOCKOUT_DURATION_MINUTES
    assert said.group(2) == "", "an account with no authenticator was pointed at the code"
    assert _row(member)["locked_until"], "a locked caller unlocked another account"


def test_a_locked_caller_with_an_authenticator_unlocks_itself_first(client, accounts):
    """A caller whose own account is locked cannot unlock anyone else with
    its password - a locked account's password is not accepted anywhere. It
    is told so, and that it can unlock its own account first with a code;
    it does, and then its password unlocks the other account."""
    admin, member = accounts("admin", mfa=True), accounts("member")
    _lock(admin)
    _lock(member)
    r = _unlock(client, admin, member, current_password=PW)
    said = _OWN_LOCK.fullmatch(r.json().get("detail") or "")
    assert r.status_code == 429 and said, r.text
    assert said.group(2) == (" You can unlock your own account first, with a current "
                             "authenticator code.")
    assert _row(member)["locked_until"] and _row(admin)["locked_until"]
    assert _passwords_counted(admin) == 0, "a password was counted for a locked caller"

    r = _unlock(client, admin, admin, mfa_code=pyotp.TOTP(admin["seed"]).now())
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    r = _unlock(client, admin, member, current_password=PW)
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert not _row(member)["locked_until"]


# -- FIX: a password that cannot be encoded -----------------------------------

def test_a_sign_in_password_that_cannot_be_encoded_is_a_wrong_password(client, accounts,
                                                                       monkeypatch):
    """JSON can carry text that is not valid Unicode (half of a surrogate
    pair), and such text cannot be encoded for the hash. At the sign-in it is
    a wrong password: the same answer, one miss counted each time, the lock
    at the limit - and the same answer for a username that does not exist.
    And the same COST: one hash round is paid for each of them, as for any
    wrong password, so the answer's timing says nothing either."""
    member = accounts("member")
    rounds = _HashRounds(monkeypatch)
    answers = []
    for name in [member["name"]] * LIMIT + ["utp_nobody"]:
        body = json.dumps({"username": name, "password": CANNOT_BE_ENCODED})
        try:
            r = client.post("/api/auth/login", content=body,
                            headers={"Content-Type": "application/json"})
            answers.append((r.status_code, r.json()))
        except Exception as exc:          # the route raised: a server error
            answers.append(("error", type(exc).__name__))
    assert answers == [(401, SIGN_IN_REFUSED)] * (LIMIT + 1), answers
    assert rounds.paid == LIMIT + 1, \
        f"{rounds.paid} hash rounds were paid for {LIMIT + 1} sign-ins; one each is the rule"
    row = _row(member)
    assert row["failed_attempts"] == LIMIT and row["locked_until"]


def _send(client, method, path, headers, body):
    """One request whose JSON body may carry text that cannot be encoded.
    Returns (status, body), or ("error", name) if the route raised."""
    try:
        r = client.request(method, path, content=json.dumps(body),
                           headers={**headers, "Content-Type": "application/json"})
        return r.status_code, r.json()
    except Exception as exc:
        return "error", type(exc).__name__


def test_a_new_password_that_cannot_be_encoded_is_refused_in_words(client, accounts):
    """Every route that sets a password asks the policy first, and the policy
    refuses text that cannot be encoded: a string 400, nothing stored. The
    two routes a signed-in caller can reach are driven here; the control at
    the end of this file reads from the source that every function that
    hashes a new password asks the policy about it first."""
    assert validate_password(NEW_PW_CANNOT_BE_ENCODED) == [NOT_STORABLE["detail"]]
    assert validate_password(CANNOT_BE_ENCODED) == [NOT_STORABLE["detail"]],         "the other rules were reported for text that cannot be stored at all"
    owner = accounts("owner")
    before = _row(owner)["password_hash"]
    answer = _send(client, "PATCH", "/api/auth/me/password", owner["headers"],
                   {"current_password": PW, "new_password": NEW_PW_CANNOT_BE_ENCODED})
    assert answer == (400, NOT_STORABLE), answer
    assert _row(owner)["password_hash"] == before
    answer = _send(client, "POST", "/api/users", owner["headers"],
                   {"username": "utp_never", "password": NEW_PW_CANNOT_BE_ENCODED,
                    "role": "member", "current_password": PW})
    assert answer == (400, NOT_STORABLE), answer
    assert get_user_by_username("utp_never") is None


# -- CONTROL ------------------------------------------------------------------

def test_control_an_administrator_with_the_right_password_unlocks_and_the_person_signs_in(
        client, accounts):
    admin, member = accounts("admin"), accounts("member")
    for _ in range(LIMIT):
        assert _sign_in(client, member, NOT_IT).status_code == 401
    assert _row(member)["locked_until"]
    assert _sign_in(client, member, PW).status_code == 401
    r = _unlock(client, admin, member, current_password=PW)
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert _sign_in(client, member, PW).status_code == 200


def test_control_the_ceiling_answers_as_it_did(client, accounts):
    """Only an Owner unlocks an Owner's account: an Admin with the right
    password is refused 403 and the lock stays; another Owner unlocks it."""
    owner, other_owner, admin = accounts("owner"), accounts("owner"), accounts("admin")
    _lock(owner)
    r = _unlock(client, admin, owner, current_password=PW)
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "Only an Owner can unlock an Owner's account"}
    assert _row(owner)["locked_until"], "a refused unlock cleared the lock"
    r = _unlock(client, other_owner, owner, current_password=PW)
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert not _row(owner)["locked_until"]


def test_control_the_sign_in_answers_and_locks_as_before(client, accounts):
    member = accounts("member")
    for n in range(1, LIMIT + 1):
        r = _sign_in(client, member, NOT_IT)
        assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text
        assert _row(member)["failed_attempts"] == n
    assert _row(member)["locked_until"], "the sign-in's misses did not lock the account"
    r = _sign_in(client, member, PW)
    assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text
    unlock_user(member["id"])                     # the lock ends
    assert _sign_in(client, member, PW).status_code == 200
    assert _row(member)["failed_attempts"] == 0


def test_control_an_account_without_manage_users_is_refused_as_before(client, accounts):
    member, other = accounts("member"), accounts("member")
    _lock(other)
    for target in (other, member):
        r = _unlock(client, member, target, current_password=PW)
        assert r.status_code == 403, r.text
        assert r.json() == {"detail": "Permission required: manage_users"}
    assert _row(other)["locked_until"]


class _Hashes(ast.NodeVisitor):
    """In one module: for each function, the arguments handed to
    validate_password and to hash_password, in source order."""

    def __init__(self):
        self.inside = ["(module level)"]
        self.calls = {}

    def visit_FunctionDef(self, node):
        self.inside.append(node.name)
        self.generic_visit(node)
        self.inside.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = getattr(node.func, "id", getattr(node.func, "attr", None))
        if name in ("validate_password", "hash_password"):
            # The password, whether it was passed by position or by name.
            given = node.args[0] if node.args else (
                node.keywords[0].value if node.keywords else None)
            self.calls.setdefault(self.inside[-1], []).append(
                (node.lineno, name, ast.unparse(given) if given is not None else ""))
        self.generic_visit(node)


def test_control_every_new_password_is_put_to_the_policy_before_it_is_hashed():
    """Read from the source, not from a list kept by hand: in every function
    under app/ that hashes a password, the same expression was handed to
    validate_password earlier in that function. (The one hash at module
    level is the sign-in's cost equalizer, of random bytes.) So the policy's
    refusal of text that cannot be encoded reaches every route that sets a
    password, the first-Owner claim included."""
    app_dir = Path(jwt_auth.__file__).resolve().parent
    hashed, unasked = 0, []
    for path in sorted(app_dir.rglob("*.py")):
        visitor = _Hashes()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig")))
        for func, calls in visitor.calls.items():
            if func == "(module level)":
                continue
            asked = set()
            for _, name, arg in sorted(calls):
                if name == "validate_password":
                    asked.add(arg)
                else:
                    hashed += 1
                    if arg not in asked:
                        unasked.append((path.relative_to(app_dir).as_posix(), func, arg))
    assert hashed >= 3, "the scan found fewer hashes than the routes that set a password"
    assert unasked == [], unasked
    # Two ways round the scan above, closed: a hash made by calling bcrypt
    # directly, and one of these names imported under another. (Not closed:
    # a function bound to a new name by assignment, or reached by getattr.)
    direct = [(where, func) for where, func, name in _calls_under_app({"hashpw"})]
    assert direct == [("jwt_auth.py", "hash_password")], direct
    assert _aliases_under_app(_WATCHED_NAMES) == []


_WATCHED_NAMES = {"hash_password", "validate_password", "hashpw", "unlock_user",
                  "reset_failed_attempts", "clear_failed_attempts_unless_locked",
                  "username_refusal"}


def _calls_under_app(names):
    """(module, function it is in, name called) for every call of one of
    `names` under app/, by plain name or as an attribute."""
    app_dir = Path(jwt_auth.__file__).resolve().parent
    found = []

    class Calls(ast.NodeVisitor):
        def __init__(self):
            self.inside = ["(module level)"]

        def visit_FunctionDef(self, node):
            self.inside.append(node.name)
            self.generic_visit(node)
            self.inside.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name in names:
                found.append((where, self.inside[-1], name))
            self.generic_visit(node)

    for path in sorted(app_dir.rglob("*.py")):
        where = path.relative_to(app_dir).as_posix()
        Calls().visit(ast.parse(path.read_text(encoding="utf-8-sig")))
    return found


def _aliases_under_app(names):
    """Every `import ... as` under app/ that gives one of `names` another
    name - which the scans here, reading calls by name, would not see."""
    app_dir = Path(jwt_auth.__file__).resolve().parent
    found = []
    for path in sorted(app_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found += [(path.relative_to(app_dir).as_posix(), alias.name, alias.asname)
                          for alias in node.names
                          if alias.asname and alias.name.split(".")[-1] in names]
    return found


# What may clear a lock, or the count of misses behind it, and why. A new
# caller fails the case below until it is named here with its reason - and
# its reason has to be a proof, or the passing of time.
_MAY_CLEAR_A_LOCK = {
    ("jwt_auth.py", "_lockout_check"): "a lock whose time has passed is cleared when it is next looked at",
    ("jwt_auth.py", "require_step_up"): "after the account's own right password, at a step-up door",
    ("jwt_auth.py", "_check_current_code"): "after a right authenticator code",
    ("routers/auth.py", "login"): "a lock whose time has passed; and the count, after a whole sign-in",
    ("routers/auth.py", "mfa_complete"): "the same, at the sign-in's second step, after a right code",
    ("routers/users.py", "admin_unlock_user"): "after the proof the cases above pin",
}


def test_control_every_function_that_clears_a_lock_is_named_here_with_its_reason():
    """Read from the source: the functions under app/ that call unlock_user,
    reset_failed_attempts or clear_failed_attempts_unless_locked are exactly
    the ones named above. The unlock route is pinned by its own cases; this
    is what fails if a SECOND way to clear a lock appears beside it. And the
    lock's two columns are written by app/users.py alone - a write made
    around those three functions is refused here too."""
    callers = {(where, func) for where, func, _ in _calls_under_app(
        {"unlock_user", "reset_failed_attempts", "clear_failed_attempts_unless_locked"})}
    assert callers == set(_MAY_CLEAR_A_LOCK), sorted(callers ^ set(_MAY_CLEAR_A_LOCK))
    app_dir = Path(jwt_auth.__file__).resolve().parent
    writes = []
    for path in sorted(app_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in ("update", "values"):
                keys = {k.value for a in node.args if isinstance(a, ast.Dict)
                        for k in a.keys if isinstance(k, ast.Constant)}
                keys |= {kw.arg for kw in node.keywords}
                if keys & {"locked_until", "failed_attempts"}:
                    writes.append(path.relative_to(app_dir).as_posix())
    assert set(writes) == {"users.py"}, sorted(set(writes))


# What creates an account or renames one, and where the name is asked about.
_NAME_WRITERS = {"create_user", "claim_first_owner", "update_user_username"}
_NAME_ASKED_ELSEWHERE = {
}


def test_control_every_function_that_writes_a_username_asks_about_it_first():
    """Read from the source: every function under app/ that creates an
    account or renames one calls jwt_auth.username_refusal earlier in the same
    function, or is named above with where the name is asked about instead.
    So the refusal of a name that cannot be encoded reaches the first
    Owner's claim too, which no case here can drive (it is closed
    once that account exists)."""
    asks = {"username_refusal"}
    calls = _calls_under_app(_NAME_WRITERS | asks)
    writers = {(where, func) for where, func, name in calls if name in _NAME_WRITERS
               and not (where == "users.py")}
    asked = {(where, func) for where, func, name in calls if name in asks}
    assert len(writers) >= 3, writers
    unasked = writers - asked - set(_NAME_ASKED_ELSEWHERE)
    assert unasked == set(), sorted(unasked)
    stale = set(_NAME_ASKED_ELSEWHERE) - writers
    assert stale == set(), sorted(stale)


# -- FIX: an own-account unlock is allowed once per lockout window ------------

_ONCE = re.compile(r"Your account was already unlocked from its own session in this lockout "
                   r"window, and that is allowed once per window\. Try again in (\d+) "
                   r"minute\(s\), or ask (.+) to unlock it\.")


def _is_once_per_window(response, who_else):
    """The count's own refusal: 429, the reason in words, a whole number of
    minutes no longer than the window, and whom to ask."""
    detail = response.json().get("detail")
    said = _ONCE.fullmatch(detail) if isinstance(detail, str) else None
    return (response.status_code == 429 and said is not None
            and 1 <= int(said.group(1)) <= LOCKOUT_DURATION_MINUTES
            and said.group(2) == who_else)


def _right_code(account):
    return pyotp.TOTP(account["seed"]).now()


def test_the_second_own_account_unlock_in_a_window_is_refused(client, accounts):
    """One unlock of your own account per lockout window. The second is
    refused - a right code, a wrong one and none at all alike - with 429, in
    words, with the minutes and whom to ask. It is refused BEFORE the code is
    read: nothing is counted in the code bound, and the lock stays."""
    top = accounts("owner", mfa=True)
    _lock(top)
    r = _unlock(client, top, top, mfa_code=_right_code(top))
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert _own_unlocks_counted(top) == 1
    _lock(top)
    for body in ({"mfa_code": _right_code(top)}, {"mfa_code": _wrong_code(top["seed"])}, {}):
        r = _unlock(client, top, top, **body)
        assert _is_once_per_window(r, "another Owner"), (body, r.status_code, r.text)
    assert _codes_counted(top) == 0, "a code was counted for a request refused before it was read"
    assert _own_unlocks_counted(top) == 1
    row = _row(top)
    assert row["locked_until"] and row["failed_attempts"] == LIMIT


def test_the_loop_of_unlock_and_guess_is_bounded(client, accounts, monkeypatch):
    """The party this count is for holds a live session and the account's
    authenticator, and not its password: it guesses at the sign-in until the
    lock lands, unlocks its own account with a right code, and guesses again.
    One unlock per window makes that LIMIT guesses before the lock and LIMIT
    after the one unlock, and then none. Counted here as the sign-ins sent
    while no lock stood - each of those was checked - across four rounds,
    with the per-address throttle taken out of the way so that what stops
    the loop is the account's own bound."""
    monkeypatch.setattr(security, "AUTH_MAX_ATTEMPTS", 100 * LIMIT)
    top = accounts("owner", mfa=True)
    checked, unlocks = 0, []
    for _ in range(4):
        while not _row(top)["locked_until"]:
            assert checked < 4 * LIMIT, "the lock never landed"
            r = _sign_in(client, top, NOT_IT)
            assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text
            checked += 1
        unlocks.append(_unlock(client, top, top, mfa_code=_right_code(top)).status_code)
    assert unlocks == [200, 429, 429, 429], unlocks
    assert checked == 2 * LIMIT, \
        f"{checked} passwords were checked across the loop; the bound is {2 * LIMIT}"
    r = _sign_in(client, top, PW)
    assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text


def test_a_wrong_code_does_not_use_up_the_windows_one_unlock(client, accounts):
    """What is counted is unlocks. A request whose code is wrong or missing
    unlocked nothing, so the place it took is handed back, and the right
    code after it still unlocks."""
    top = accounts("owner", mfa=True)
    _lock(top)
    r = _unlock(client, top, top, mfa_code=_wrong_code(top["seed"]))
    assert r.status_code == 400 and r.json() == WRONG_CODE, r.text
    r = _unlock(client, top, top)
    assert r.status_code == 400 and r.json() == CODE_REQUIRED, r.text
    assert _own_unlocks_counted(top) == 0, "a request that unlocked nothing was counted as an unlock"
    r = _unlock(client, top, top, mfa_code=_right_code(top))
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert _own_unlocks_counted(top) == 1


def test_a_request_refused_by_the_code_bound_does_not_use_the_unlock_up(client, accounts,
                                                                        monkeypatch):
    monkeypatch.setattr(security, "TOTP_MAX_FAILURES", 1)
    top = accounts("owner", mfa=True)
    _lock(top)
    r = _unlock(client, top, top, mfa_code=_wrong_code(top["seed"]))
    assert r.status_code == 400 and r.json() == WRONG_CODE, r.text
    r = _unlock(client, top, top, mfa_code=_right_code(top))
    assert r.status_code == 429, r.text
    assert r.json()["detail"].startswith("Too many wrong authenticator codes on this account."), r.text
    assert _own_unlocks_counted(top) == 0


def test_after_the_window_an_own_account_unlock_works_again(client, accounts):
    """The count ends with its window and with nothing else."""
    top = accounts("owner", mfa=True)
    _lock(top)
    assert _unlock(client, top, top, mfa_code=_right_code(top)).status_code == 200
    _lock(top)
    assert _is_once_per_window(_unlock(client, top, top, mfa_code=_right_code(top)), "another Owner")
    # The count's row as it reads once the lockout window has passed.
    state_store.put(f"own_unlock:{top['id']}", {"n": 1, "since": 0.0}, ttl=-1)
    r = _unlock(client, top, top, mfa_code=_right_code(top))
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert not _row(top)["locked_until"]


def test_an_administrators_unlock_of_the_same_account_is_not_counted(client, accounts):
    """An administrator's unlock of ANOTHER account takes the administrator's
    own password and is not limited: it is not counted in the own-account
    count, however often it happens, and it does not clear that count
    either."""
    other, top = accounts("owner"), accounts("owner", mfa=True)
    for _ in range(2):
        _lock(top)
        r = _unlock(client, other, top, current_password=PW)
        assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert _own_unlocks_counted(top) == 0, "an administrator's unlock was counted as the account's own"
    _lock(top)
    assert _unlock(client, top, top, mfa_code=_right_code(top)).status_code == 200
    _lock(top)
    r = _unlock(client, other, top, current_password=PW)
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert _own_unlocks_counted(top) == 1, "an administrator's unlock cleared the account's own count"
    _lock(top)
    assert _is_once_per_window(_unlock(client, top, top, mfa_code=_right_code(top)), "another Owner")


# -- FIX: a code that cannot be encoded is a wrong code -----------------------

CODE_CANNOT_BE_ENCODED = "12345" + chr(0xD800)


def test_a_code_that_cannot_be_encoded_is_a_wrong_code_at_the_own_account_unlock(client, accounts):
    """It can match no code, and the comparison raised on it: a server
    error, counted in the code bound and nowhere else. It is a wrong code:
    the same 400, a miss toward the lock when no lock stands, and with the
    lock standing the lock left as it is."""
    mid = accounts("admin", mfa=True)
    path = f"/api/admin/users/{mid['id']}/unlock"
    answer = _send(client, "POST", path, mid["headers"], {"mfa_code": CODE_CANNOT_BE_ENCODED})
    assert answer == (400, WRONG_CODE), answer
    assert _row(mid)["failed_attempts"] == 1 and _codes_counted(mid) == 1
    _lock(mid)
    locked = _row(mid)
    answer = _send(client, "POST", path, mid["headers"], {"mfa_code": CODE_CANNOT_BE_ENCODED})
    assert answer == (400, WRONG_CODE), answer
    after = _row(mid)
    assert (after["locked_until"], after["failed_attempts"]) == \
        (locked["locked_until"], locked["failed_attempts"])
    assert _codes_counted(mid) == 2 and _own_unlocks_counted(mid) == 0


def test_a_code_that_cannot_be_encoded_is_a_wrong_code_at_the_sign_in(client, accounts):
    low = accounts("member", mfa=True)
    r = _sign_in(client, low, PW)
    assert r.status_code == 200 and r.json().get("mfa_required"), r.text
    answer = _send(client, "POST", "/api/auth/mfa/complete", {},
                   {"mfa_token": r.json()["mfa_token"], "code": CODE_CANNOT_BE_ENCODED})
    assert answer == (401, WRONG_CODE), answer
    assert _row(low)["failed_attempts"] == 1 and _codes_counted(low) == 1


def test_a_code_that_cannot_be_encoded_is_a_wrong_code_at_the_enrollment_confirm(client, accounts):
    low = accounts("member")
    r = client.post("/api/auth/mfa/setup", headers=low["headers"], json={"current_password": PW})
    assert r.status_code == 200, r.text
    answer = _send(client, "POST", "/api/auth/mfa/enable", low["headers"],
                   {"code": CODE_CANNOT_BE_ENCODED})
    assert answer == (400, WRONG_CODE), answer
    assert not _row(low)["mfa_enabled"] and _codes_counted(low) == 1


# -- FIX: the target is read after the proof, whatever its status -------------

NO_SUCH_USER = {"detail": "No such user"}


def test_an_id_that_names_no_account_is_a_404_after_the_proof(client, accounts):
    """The unlock wrote by id and answered "unlocked" for an id that names
    no account. It is a 404 - and only AFTER the proof, so a caller who
    fails the proof learns nothing about which ids exist."""
    mid = accounts("admin")
    nobody = {"id": _row(mid)["id"] + 1_000_000}
    r = _unlock(client, mid, nobody)
    assert r.status_code == 400 and r.json() == PW_REQUIRED, r.text
    r = _unlock(client, mid, nobody, current_password=NOT_IT)
    assert r.status_code == 400 and r.json() == WRONG_PW, r.text
    r = _unlock(client, mid, nobody, current_password=PW)
    assert r.status_code == 404 and r.json() == NO_SUCH_USER, r.text


def test_the_ceiling_holds_for_a_deactivated_account(client, accounts):
    """The ceiling read the target through a reader that hides a deactivated
    row, and the write did not: a caller below the top tier, with its own
    password, cleared the lock on a deactivated top-tier account. The target
    is read whatever its status, so the ceiling refuses it there too; the
    top tier still unlocks it."""
    top, other, mid = accounts("owner"), accounts("owner"), accounts("admin")
    _lock(top)
    deactivate_user(top["id"])
    r = _unlock(client, mid, top, current_password=PW)
    assert r.status_code == 403 and r.json() == {"detail": "Only an Owner can unlock an Owner's account"}, r.text
    assert _lock_of(top), "a refused unlock cleared a deactivated account's lock"
    r = _unlock(client, other, top, current_password=PW)
    assert r.status_code == 200 and r.json() == UNLOCKED, r.text
    assert not _lock_of(top)


# -- FIX: a username that cannot be encoded -----------------------------------

NAME_CANNOT_BE_ENCODED = "utp" + chr(0xD800)
NAME_NOT_STORABLE = {"detail": "Username contains a character that cannot be stored. "
                               "Choose a different username"}


def test_a_sign_in_username_that_cannot_be_encoded_is_an_unknown_username(client, monkeypatch):
    """Such a name can be no account's, and looking it up raised: a server
    error, after the per-address count. It is an unknown username: the same
    401, at the same cost - the one hash round the sign-in pays for a name
    it does not know."""
    rounds = _HashRounds(monkeypatch)
    answer = _send(client, "POST", "/api/auth/login", {},
                   {"username": NAME_CANNOT_BE_ENCODED, "password": NOT_IT})
    assert answer == (401, SIGN_IN_REFUSED), answer
    assert rounds.paid == 1, rounds.paid


def test_a_new_username_that_cannot_be_encoded_is_refused_in_words(client, accounts):
    """Creating an account under such a name, and renaming your own to it,
    raised at the write. Both are a string 400 now and nothing is written.
    (The first Owner's claim asks the same question; the control
    above reads that from the source.)"""
    assert jwt_auth.username_refusal(NAME_CANNOT_BE_ENCODED) == NAME_NOT_STORABLE["detail"]
    assert jwt_auth.username_refusal("an-ordinary-name") is None
    top = accounts("owner")
    answer = _send(client, "POST", "/api/users", top["headers"],
                   {"username": NAME_CANNOT_BE_ENCODED, "password": NEW_PW,
                    "role": "member", "current_password": PW})
    assert answer == (400, NAME_NOT_STORABLE), answer
    answer = _send(client, "PATCH", "/api/auth/me/username", top["headers"],
                   {"new_username": NAME_CANNOT_BE_ENCODED, "current_password": PW})
    assert answer == (400, NAME_NOT_STORABLE), answer
    assert _row(top)["username"] == top["name"]
