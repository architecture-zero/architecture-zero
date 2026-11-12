"""Changing your own password counts toward the account lock.

PATCH /api/auth/me/password takes the caller's current password. It used to
check it with a bare verify_password: a wrong one answered 400 and nothing
else happened. The miss was never counted and a locked account was not
refused, so a session alone could test guesses for the current password here
with no bound - while every other door that takes the caller's own password
goes through jwt_auth.require_step_up, which counts each miss against the
same failed_attempts / locked_until the sign-in uses. The current password is
the proof at every door that changes durable authority, so the one door that
did not count was the way round all the others. The route now calls the
helper as its first statement.

FIX (each fails on the route as it was):
  - each wrong current password raises the account's failed-attempt count by
    one
  - the miss that reaches MAX_LOGIN_ATTEMPTS answers 429 and locks the account
  - a locked account is refused with 429 even with the RIGHT current
    password, and its stored hash and its sessions are untouched
  - once locked here, the sign-in refuses too: one counter, not two
  - a right change clears the count
  - the miss leaves the helper's auth_step_up_failed log row with the action
  - a missing current password is a string 400, never a 422, and neither it
    nor an empty one is counted as a guess
  - an account with no usable password is told so, not that its password is
    incorrect
  - no module but jwt_auth calls verify_password: the sign-in and the helper
    are the only two callers, and both count
CONTROL (true before and after, by design):
  - a plain wrong password is still 400 with the same string detail - not
    401, not 422 - and changes nothing
  - a wrong current password sent with a new one that breaks the policy still
    answers "Current password is incorrect", not the policy's errors
  - a right change still stores the new hash, revokes every refresh token and
    returns the same body
  - a right current password with a new one that breaks the policy still
    answers the policy's errors with 400

Every case drives the app's own route through the suite's test client, on a
throwaway account of its own.
"""
import ast
import itertools
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import jwt_auth
from app.jwt_auth import (MAX_LOGIN_ATTEMPTS, create_access_token, hash_password,
                          unusable_password_hash, validate_password, verify_password)
from app.logger import get_logger
from app.users import create_user, get_user_by_id, lock_user, unlock_user

ROUTE = "/api/auth/me/password"
OLD_PW = "OldPassw0rd!x"
NEW_PW = "NewPassw0rd!y"
NOT_IT = "not-the-password"
BAD_NEW = "x"                     # shorter than any minimum length
WRONG = "Current password is incorrect"
CHANGED = {"status": "password updated - please sign in again"}

_names = itertools.count(1)


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def account(client):
    """A throwaway account with a known password, signed in. Each test gets
    its own, so a lock one test lands is never another test's problem."""
    assert validate_password(NEW_PW) == [] and validate_password(BAD_NEW)
    name = f"opl_{next(_names)}"
    uid = create_user(name, hash_password(OLD_PW))
    yield {"id": uid, "name": name, "headers": _login(client, name, OLD_PW)}
    unlock_user(uid)


def _change(client, account, **body):
    return client.patch(ROUTE, headers=account["headers"], json=body)


def _row(account):
    return get_user_by_id(account["id"])


def _refresh_rows(uid):
    from app.db import get_session
    from app.models import RefreshToken
    with get_session() as db:
        return [(rt.token_hash, rt.revoked) for rt in
                db.query(RefreshToken).filter(RefreshToken.user_id == uid).all()]


class _Rows(logging.Handler):
    """What the app's own log was handed, as (event, fields)."""

    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append((record.getMessage(), dict(getattr(record, "data", {}))))


@pytest.fixture
def app_log():
    handler = _Rows()
    logger = get_logger()
    logger.addHandler(handler)
    yield handler.rows
    logger.removeHandler(handler)


# -- FIX: a wrong current password is counted, and the count locks -------------

def test_each_wrong_current_password_raises_the_failed_attempt_count_by_one(client, account):
    assert _row(account)["failed_attempts"] == 0
    for expected in range(1, MAX_LOGIN_ATTEMPTS + 1):
        _change(client, account, current_password=NOT_IT, new_password=NEW_PW)
        assert _row(account)["failed_attempts"] == expected, \
            "a wrong current password was not counted against the account"


def test_the_miss_that_reaches_the_threshold_answers_429_and_locks_the_account(client, account):
    for miss in range(1, MAX_LOGIN_ATTEMPTS + 1):
        r = _change(client, account, current_password=NOT_IT, new_password=NEW_PW)
        if miss < MAX_LOGIN_ATTEMPTS:
            assert r.status_code == 400 and r.json()["detail"] == WRONG, (miss, r.text)
            assert not _row(account)["locked_until"], f"locked at miss {miss}, below the threshold"
        else:
            assert r.status_code == 429, (miss, r.text)
            assert isinstance(r.json()["detail"], str)
    assert _row(account)["locked_until"], "no lock was written"


def test_a_locked_account_is_refused_even_with_the_right_password(client, account):
    """The lock is a refusal, not a slower guess: the right current password
    does not get through it, and nothing about the account moves."""
    before_hash = _row(account)["password_hash"]
    before_sessions = _refresh_rows(account["id"])
    lock_user(account["id"],
              (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
    r = _change(client, account, current_password=OLD_PW, new_password=NEW_PW)
    assert r.status_code == 429, r.text
    assert isinstance(r.json()["detail"], str) and "locked" in r.json()["detail"].lower()
    assert _row(account)["password_hash"] == before_hash, "a locked account's password was changed"
    assert _refresh_rows(account["id"]) == before_sessions, "a refused change revoked the sessions"


def test_once_locked_here_the_sign_in_refuses_too(client, account):
    """One counter, not two: the lock this route lands is the sign-in's lock."""
    for _ in range(MAX_LOGIN_ATTEMPTS):
        _change(client, account, current_password=NOT_IT, new_password=NEW_PW)
    sign_in = {"username": account["name"], "password": OLD_PW}
    assert client.post("/api/auth/login", json=sign_in).status_code == 401, \
        "the right password signed in to an account this route should have locked"
    # It was the lock that refused, and nothing else: lifted, the same
    # password signs in.
    unlock_user(account["id"])
    assert client.post("/api/auth/login", json=sign_in).status_code == 200


def test_a_right_change_clears_the_count(client, account):
    """The count a wrong SIGN-IN left is the one a right change here clears."""
    r = client.post("/api/auth/login", json={"username": account["name"], "password": NOT_IT})
    assert r.status_code == 401
    assert _row(account)["failed_attempts"] == 1
    r = _change(client, account, current_password=OLD_PW, new_password=NEW_PW)
    assert r.status_code == 200, r.text
    assert _row(account)["failed_attempts"] == 0, "a right change left the failed-attempt count standing"


def test_the_miss_leaves_the_step_up_failed_log_row_with_the_action(client, account, app_log):
    _change(client, account, current_password=NOT_IT, new_password=NEW_PW)
    misses = [fields for event, fields in app_log if event == "auth_step_up_failed"]
    assert misses == [{"user_id": account["id"], "action": "change your password",
                       "how": "password", "attempts": 1}], app_log


def test_a_missing_current_password_is_a_string_400_and_no_guess(client, account):
    """A required field would answer 422 with a list-shaped detail, which a
    client that renders `detail` raw cannot show. And a request that carries
    no password is not a guess at one."""
    for body in ({"new_password": NEW_PW}, {"current_password": "", "new_password": NEW_PW},
                 {"current_password": "   ", "new_password": NEW_PW}):
        r = _change(client, account, **body)
        assert r.status_code == 400, (body, r.text)
        detail = r.json()["detail"]
        assert isinstance(detail, str) and "required" in detail.lower(), (body, detail)
    assert _row(account)["failed_attempts"] == 0
    assert verify_password(OLD_PW, _row(account)["password_hash"])


def test_an_account_with_no_usable_password_is_told_so(client):
    """Such an account has no current password to give. It used to be told
    the one it sent was incorrect; the helper names the real reason."""
    name = f"opl_{next(_names)}"
    uid = create_user(name, unusable_password_hash())
    row = get_user_by_id(uid)
    headers = {"Authorization": f"Bearer {create_access_token(uid, name, row['role'])}"}
    r = client.patch(ROUTE, headers=headers,
                     json={"current_password": NOT_IT, "new_password": NEW_PW})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, str) and detail != WRONG
    assert "cannot change your password" in detail, detail
    after = get_user_by_id(uid)
    assert after["password_hash"] == row["password_hash"] and after["failed_attempts"] == 0


# The three names that can test a password. verify_password is the bare check
# (bcrypt's checkpw is what it calls); authenticate_user is the sign-in's
# check and counts nothing by itself - its one caller, the login route, does.
_WATCHED = ("verify_password", "checkpw", "authenticate_user")
_ALLOWED_USES = {
    ("jwt_auth.py", "checkpw", "verify_password"),
    ("jwt_auth.py", "verify_password", "authenticate_user"),
    ("jwt_auth.py", "verify_password", "require_step_up"),
    ("routers/auth.py", "authenticate_user", "(module level)"),   # its import
    ("routers/auth.py", "authenticate_user", "login"),
}


class _PasswordCheckUses(ast.NodeVisitor):
    """Every USE of one of those names in one module, by the function it is
    in: a call, a bare reference (bound to another name, handed on as a
    callback, wrapped in a partial), an attribute lookup, an import under any
    name, and the name as a string (a getattr). Not a `def` of the name."""

    def __init__(self):
        self.inside = ["(module level)"]
        self.found = set()

    def visit_FunctionDef(self, node):
        self.inside.append(node.name)
        self.generic_visit(node)
        self.inside.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _use(self, name):
        if name in _WATCHED:
            self.found.add((name, self.inside[-1]))

    def visit_Name(self, node):
        self._use(node.id)

    def visit_Attribute(self, node):
        self._use(node.attr)
        self.generic_visit(node)

    def visit_alias(self, node):
        self._use(node.name.rsplit(".", 1)[-1])

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self._use(node.value)


def _names_used(source):
    visitor = _PasswordCheckUses()
    visitor.visit(ast.parse(source))
    return {name for name, _ in visitor.found}


def test_only_the_sign_in_and_the_step_up_helper_verify_a_password():
    """verify_password is the bare check: it answers and counts nothing. Its
    two callers, the sign-in's authenticate_user and the step-up helper, live
    beside it in jwt_auth and every miss at either is counted; the sign-in's
    check has one caller, the login route, which counts. Any other use of one
    of the three names anywhere in app/ - a call, an import, a reference
    handed on - is a door that could test a password without counting."""
    app_dir = Path(jwt_auth.__file__).resolve().parent
    uses = set()
    for path in sorted(app_dir.rglob("*.py")):
        visitor = _PasswordCheckUses()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig")))
        uses |= {(path.relative_to(app_dir).as_posix(), name, func)
                 for name, func in visitor.found}
    assert uses == _ALLOWED_USES, sorted(uses ^ _ALLOWED_USES)


@pytest.mark.parametrize("source", [
    "from app.jwt_auth import verify_password as check\ncheck(a, b)\n",
    "import app.jwt_auth as j\nok = j.verify_password\n",
    "from app import jwt_auth\nf = getattr(jwt_auth, 'verify_password')\n",
    "import functools, app.jwt_auth as j\np = functools.partial(j.verify_password, pw)\n",
    "import bcrypt\nbcrypt.checkpw(a, b)\n",
    "from app.jwt_auth import authenticate_user\nuser = authenticate_user(n, p)\n",
])
def test_control_the_scan_sees_a_check_that_is_not_a_plain_call(source):
    """The scan's own instrument, on the shapes a scan for direct calls of
    verify_password misses: an import under another name, a reference bound
    or handed on, a getattr, a partial, bcrypt's own check, and the sign-in's
    check called from somewhere new."""
    assert _names_used(source), source

# -- CONTROL: what the route answered before, it still answers ----------------

def test_a_plain_wrong_password_is_still_a_string_400(client, account):
    """Not 401 - a client that signs out on 401 would sign the person out for
    a typo - and not 422."""
    before = _row(account)["password_hash"]
    r = _change(client, account, current_password=NOT_IT, new_password=NEW_PW)
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": WRONG}
    assert _row(account)["password_hash"] == before


def test_a_wrong_current_password_learns_nothing_about_the_policy(client, account):
    """The current password is checked before the new one is validated."""
    r = _change(client, account, current_password=NOT_IT, new_password=BAD_NEW)
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": WRONG}


def test_a_right_change_still_stores_the_new_hash_and_revokes_every_session(client, account):
    _login(client, account["name"], OLD_PW)          # a second session
    before = _refresh_rows(account["id"])
    assert len(before) == 2 and not any(revoked for _, revoked in before)
    r = _change(client, account, current_password=OLD_PW, new_password=NEW_PW)
    assert r.status_code == 200, r.text
    assert r.json() == CHANGED
    stored = _row(account)["password_hash"]
    assert verify_password(NEW_PW, stored) and not verify_password(OLD_PW, stored)
    after = _refresh_rows(account["id"])
    assert [h for h, _ in after] == [h for h, _ in before]
    assert all(revoked for _, revoked in after), "a refresh token survived the change"
    old = client.post("/api/auth/login", json={"username": account["name"], "password": OLD_PW})
    new = client.post("/api/auth/login", json={"username": account["name"], "password": NEW_PW})
    assert (old.status_code, new.status_code) == (401, 200)


def test_a_right_password_with_a_new_one_that_breaks_the_policy_answers_the_policy(client, account):
    before = _row(account)["password_hash"]
    r = _change(client, account, current_password=OLD_PW, new_password=BAD_NEW)
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": "; ".join(validate_password(BAD_NEW))}
    assert _row(account)["password_hash"] == before
