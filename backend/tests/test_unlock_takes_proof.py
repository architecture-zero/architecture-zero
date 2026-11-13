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
CONTROL (true before and after, by design):
  - an administrator with the right password unlocks another account, and
    that person signs in at once
  - the ceiling answers as it did
  - the sign-in's answers and its lock are unchanged for an ordinary wrong
    password
  - an account without manage_users is refused as before
  - every function that hashes a new password asks the policy about it
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

def test_a_sign_in_password_that_cannot_be_encoded_is_a_wrong_password(client, accounts):
    """JSON can carry text that is not valid Unicode (half of a surrogate
    pair), and such text cannot be encoded for the hash. At the sign-in it is
    a wrong password: the same answer, one miss counted each time, the lock
    at the limit - and the same answer for a username that does not exist."""
    member = accounts("member")
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
        if name in ("validate_password", "hash_password") and node.args:
            self.calls.setdefault(self.inside[-1], []).append(
                (node.lineno, name, ast.unparse(node.args[0])))
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
