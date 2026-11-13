"""The count of passwords checked at the step-up doors is a bound.

Every door that takes the caller's own password goes through
jwt_auth.require_step_up. What this file pins is what that helper guarantees
about the password behind those doors:

  - no more than MAX_LOGIN_ATTEMPTS passwords are checked for one account in
    one lockout window, whether they are sent one at a time or all together;
  - past that, a right password is refused like a wrong one, and nothing is
    hashed;
  - an unlock does not hand the guesses back - it clears the sign-in's lock,
    so the person can sign in, and nothing else;
  - the lock is read from the stored row, and looked at again in the
    statement that clears the count after a right password;
  - misses that overlap are each counted.

The helper counts each password in security.count_password_attempt BEFORE it
hashes it - one write, in a count only a right password clears - beside the
account lock, which is looked at before the hash and counted after it and
which an unlock clears.

FIX (each fails on the helper as it was):
  - wrong passwords sent together: no more than the limit are hashed, and
    the account ends locked
  - a right password sent while they are being checked is refused, and the
    stored hash does not change
  - passwords that arrive while one is being checked find it counted already
  - a right password after the limit, and after an unlock, is still refused,
    with a sentence that says when to try again
  - a session that holds manage_users and unlocks itself between rounds is
    stopped at the limit
  - every door that takes the password counts in the one count and is
    refused by it (the doors are found by scanning app/ for the helper's
    callers; a caller with no request here fails the scan)
  - a lock stored after the session was read is seen, and so is one stored
    while the right password was being checked
  - two misses that overlap are both counted
  - a password that cannot be encoded is a miss like any other, never a
    server error
  - only an Owner unlocks an Owner's account
  - a missing new password, or new username, is a string 400
  - an account with no usable password is told so in words that fit every
    action phrase, with no remedy named that does not exist
PINS (they held on the first version of the count too; nothing else here
failed when a copy of the helper broke one of them):
  - the window is fixed: a later attempt, counted or refused, does not
    move its end
  - a right authenticator code does not clear the count
  - with a lock stored after the session was read, nothing is hashed - for
    a wrong password as for a right one
CONTROL (true before and after, by design):
  - one wrong password at a time: the same answers as before
  - a right password clears the count; the next miss counts from one
  - a limit of zero still lets the right password through
  - the sign-in's answers and its lock are unchanged, and its misses are not
    counted at the step-up doors
  - an administrator's unlock still lets the person sign in
  - an Admin still unlocks a Member's account, and an Owner an Owner's

Every case drives the app's own routes through the suite's test client, on
throwaway accounts of its own. The cases that send requests together hold
each password check open until every request has either reached the check or
been answered, so what they assert holds for every interleaving; nothing is
measured by timing.
"""
import ast
import itertools
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import jwt_auth
from app.jwt_auth import (LOCKOUT_DURATION_MINUTES, MAX_LOGIN_ATTEMPTS,
                          create_access_token, hash_password,
                          unusable_password_hash, verify_password)
from app.users import (create_user, deactivate_user, get_user_by_id, lock_user,
                       unlock_user)

LIMIT = MAX_LOGIN_ATTEMPTS
ROUTE = "/api/auth/me/password"
PW = "OldPassw0rd!x"
NEW_PW = "NewPassw0rd!y"
NOT_IT = "not-the-password"
WRONG = "Current password is incorrect"
LOCKED_NOW = f"Too many failed attempts. Account locked for {LOCKOUT_DURATION_MINUTES} minutes."
SIGN_IN_REFUSED = {"detail": "Invalid username or password"}
BURST = 4 * LIMIT        # requests sent together; well under the server's worker threads
HOLD_SECONDS = 120       # how long a held check waits for the test before it gives up

_names = itertools.count(1)


def _account(role):
    """A throwaway account with a known password and a session of its own.
    The session's token is minted here, so no test spends the sign-in's
    per-address budget on getting one."""
    name = f"pcb_{next(_names)}"
    uid = create_user(name, hash_password(PW), role=role)
    token = create_access_token(uid, name, role)
    return {"id": uid, "name": name, "role": role,
            "headers": {"Authorization": f"Bearer {token}"}}


def _retire(account):
    unlock_user(account["id"])
    deactivate_user(account["id"])


@pytest.fixture
def accounts():
    """Makes throwaway accounts by role and retires each when the test ends,
    so no test here leaves an extra Owner or a lock behind for the next."""
    made = []

    def make(role="member"):
        made.append(_account(role))
        return made[-1]

    yield make
    for account in made:
        _retire(account)


@pytest.fixture
def member(accounts):
    return accounts("member")


def _change(client, account, password, new_password=NEW_PW):
    return client.patch(ROUTE, headers=account["headers"],
                        json={"current_password": password, "new_password": new_password})


def _sign_in(client, account, password):
    return client.post("/api/auth/login",
                       json={"username": account["name"], "password": password})


def _row(account):
    return get_user_by_id(account["id"])


def _in_30_minutes():
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()


_THE_BOUND = re.compile(r"Too many password attempts on this account\. Try again in (\d+) "
                        r"minute\(s\)\. An unlock does not shorten this wait\.")


def _is_the_bound(response):
    """The count's own refusal: 429, and one sentence that says when to try
    again - a whole number of minutes, at least one and never more than the
    window."""
    detail = response.json().get("detail")
    said = _THE_BOUND.fullmatch(detail) if isinstance(detail, str) else None
    return (response.status_code == 429 and said is not None
            and 1 <= int(said.group(1)) <= LOCKOUT_DURATION_MINUTES)


class _Checks:
    """Every password handed to the one bare check (jwt_auth.verify_password),
    in the order it arrived. With `hold`, a check for which hold(password) is
    true waits for release() before it answers - which is how a test keeps
    several checks open at once without timing anything."""

    def __init__(self, monkeypatch, hold=None):
        self._real = jwt_auth.verify_password
        self._hold = hold
        self._open = threading.Event()
        self._guard = threading.Lock()
        self.seen = []
        monkeypatch.setattr(jwt_auth, "verify_password", self._check)

    def _check(self, plain, hashed):
        with self._guard:
            self.seen.append(plain)
        if self._hold is not None and self._hold(plain):
            self._open.wait(HOLD_SECONDS)
        return self._real(plain, hashed)

    def count(self):
        with self._guard:
            return len(self.seen)

    def release(self):
        self._open.set()


def _send_together(client, account, passwords):
    """One thread per password, all started at once. Returns the threads and
    the list their answers land in: (status, detail), or ("error", text) if
    the request raised."""
    answers = [None] * len(passwords)

    def send(i):
        try:
            r = _change(client, account, passwords[i])
            answers[i] = (r.status_code, r.json().get("detail"))
        except Exception as exc:          # a server error is an answer too
            answers[i] = ("error", repr(exc))

    threads = [threading.Thread(target=send, args=(i,), daemon=True)
               for i in range(len(passwords))]
    for t in threads:
        t.start()
    return threads, answers


def _wait_until_each_is_held_or_answered(checks, answers):
    """True once every request sent is either inside the (held) password
    check or already answered - the one moment the cases below assert at."""
    end = time.monotonic() + HOLD_SECONDS
    while time.monotonic() < end:
        if checks.count() + sum(a is not None for a in answers) >= len(answers):
            return True
        time.sleep(0.01)
    return False


# -- FIX: requests that arrive together ----------------------------------------

def test_wrong_passwords_sent_together_check_no_more_than_the_limit(client, member, monkeypatch):
    """However many arrive together, the limit is how many are hashed: each
    is counted before its hash, so the ones past the limit are refused with
    nothing hashed, and the ones inside it lock the account when they land."""
    checks = _Checks(monkeypatch, hold=lambda plain: True)
    threads, answers = _send_together(client, member,
                                      [f"{NOT_IT}-{i}" for i in range(BURST)])
    try:
        assert _wait_until_each_is_held_or_answered(checks, answers), \
            "the requests did not all reach the check or an answer"
        assert checks.count() <= LIMIT, \
            f"{checks.count()} passwords were being checked at once; the limit is {LIMIT}"
    finally:
        checks.release()
        for t in threads:
            t.join(HOLD_SECONDS)
    assert not any(t.is_alive() for t in threads)
    assert checks.count() == LIMIT
    statuses = [status for status, _ in answers]
    assert "error" not in statuses and 200 not in statuses, answers
    # The ones that were hashed: all wrong, each counted, the last locks.
    assert statuses.count(400) == LIMIT - 1, answers
    assert {detail for status, detail in answers if status == 400} == {WRONG}
    assert statuses.count(429) == BURST - (LIMIT - 1), answers
    row = _row(member)
    assert row["failed_attempts"] == LIMIT, "a miss in the burst was not counted"
    assert row["locked_until"], "the burst did not leave the account locked"


def test_a_right_password_sent_while_wrong_ones_are_being_checked_is_refused(client, member, monkeypatch):
    """The wrong ones are held inside their checks, so nothing has been
    counted against the lock yet and no lock is stored. The right password
    arrives then: it is past the limit, so it is refused and not hashed."""
    before = _row(member)["password_hash"]
    checks = _Checks(monkeypatch, hold=lambda plain: plain != PW)
    threads, answers = _send_together(client, member,
                                      [f"{NOT_IT}-{i}" for i in range(BURST)])
    try:
        assert _wait_until_each_is_held_or_answered(checks, answers), \
            "the requests did not all reach the check or an answer"
        r = _change(client, member, PW)
        assert _is_the_bound(r), (r.status_code, r.text)
        assert PW not in checks.seen, "the right password was hashed past the limit"
        assert _row(member)["password_hash"] == before, "the stored hash changed"
    finally:
        checks.release()
        for t in threads:
            t.join(HOLD_SECONDS)
    assert not any(t.is_alive() for t in threads)
    assert verify_password(PW, _row(member)["password_hash"])


def test_passwords_that_arrive_while_one_is_being_checked_find_it_counted(client, member, monkeypatch):
    """The order, pinned: the count is written BEFORE the hash is read. While
    the first password is inside its check, LIMIT more arrive. The first is
    counted already, so only LIMIT - 1 of them are checked and the last is
    refused. Counted after the hash, all LIMIT would be checked."""
    real = jwt_auth.verify_password
    checked, arrived_meanwhile = [], []

    def check(plain, hashed):
        checked.append(plain)
        if len(checked) == 1:
            for i in range(LIMIT):
                arrived_meanwhile.append(_change(client, member, f"{NOT_IT}-{i}"))
        return real(plain, hashed)

    monkeypatch.setattr(jwt_auth, "verify_password", check)
    first = _change(client, member, NOT_IT)
    assert len(checked) == LIMIT, f"{len(checked)} passwords were checked; the limit is {LIMIT}"
    assert [r.status_code for r in arrived_meanwhile[:-1]] == [400] * (LIMIT - 1)
    assert _is_the_bound(arrived_meanwhile[-1]), arrived_meanwhile[-1].text
    # The first one's miss is the one that reaches the lock's threshold.
    assert first.status_code == 429 and first.json()["detail"] == LOCKED_NOW, first.text


# -- FIX: an unlock does not hand the guesses back ----------------------------

def test_a_right_password_after_the_limit_and_an_unlock_is_still_refused(client, accounts, member,
                                                                         monkeypatch):
    """The limit is reached and the account locks. An unlock then clears the
    lock - and the right password is still refused at the step-up door until
    the window ends: the answer says when, and nothing is hashed or changed."""
    for _ in range(LIMIT):
        _change(client, member, NOT_IT)
    assert _row(member)["locked_until"]
    # An administrator's unlock, through the ROUTE and on the administrator's
    # own password - this called the function behind the route, so nothing
    # here would have failed if the route also cleared the account's count
    # of passwords along with its lock.
    admin = accounts("admin")
    r = client.post(f"/api/admin/users/{member['id']}/unlock", headers=admin["headers"],
                    json={"current_password": PW})
    assert r.status_code == 200 and r.json() == {"status": "unlocked"}, r.text
    before = _row(member)["password_hash"]
    checks = _Checks(monkeypatch)
    r = _change(client, member, PW)
    assert _is_the_bound(r), (r.status_code, r.text)
    assert checks.seen == [], "a password was hashed past the limit"
    assert _row(member)["password_hash"] == before
    assert not _row(member)["locked_until"], "the count's refusal is not a new lock"


def test_a_session_that_unlocks_itself_between_rounds_is_stopped_at_the_limit(client, accounts, monkeypatch):
    """An Admin holds manage_users, and the unlock route accepts the caller's
    own id. Rounds of wrong passwords with a self-unlock between them never
    reach the lock's threshold - and are stopped by the count all the same.

    Re-pointed in place: unlocking your own account takes a current
    authenticator code (test_unlock_takes_proof.py), so the Admin here has
    an authenticator enrolled and sends a right code each round. What this
    case pins has not moved: an unlock, on whatever proof, does not clear
    the count of passwords checked."""
    import pyotp
    from app.users import enable_mfa, set_mfa_secret
    admin = accounts("admin")
    seed = pyotp.random_base32()
    set_mfa_secret(admin["id"], seed)
    enable_mfa(admin["id"])
    before = _row(admin)["password_hash"]
    checks = _Checks(monkeypatch)
    answers = []
    unlocks = []
    for _ in range(3):
        for _ in range(LIMIT - 1):
            answers.append(_change(client, admin, NOT_IT))
        r = client.post(f"/api/admin/users/{admin['id']}/unlock", headers=admin["headers"],
                        json={"mfa_code": pyotp.TOTP(seed).now()})
        unlocks.append(r.status_code)
    # Re-pointed a second time: an own-account unlock is allowed once per
    # lockout window (test_unlock_takes_proof.py), so the first is taken and
    # the two after it are refused, right code and all. The count of
    # passwords checked holds either way, which is what this case pins.
    assert unlocks == [200, 429, 429], unlocks
    assert checks.count() == LIMIT, \
        f"{checks.count()} wrong passwords were checked across the rounds; the limit is {LIMIT}"
    assert [r.status_code for r in answers[:LIMIT]] == [400] * LIMIT
    assert all(_is_the_bound(r) for r in answers[LIMIT:]), [r.text for r in answers[LIMIT:]]
    # ... and the right one is refused too.
    assert _is_the_bound(_change(client, admin, PW))
    assert _row(admin)["password_hash"] == before


# -- FIX: the same at every door that takes the password ----------------------

def _doors(other_id):
    """One request per caller of the helper, keyed (module, function): what
    reaches require_step_up there with the password given. A wrong password
    is refused before the route does anything, so none of these changes
    anything."""
    return {
        ("routers/auth.py", "mfa_setup"): lambda pw: (
            "POST", "/api/auth/mfa/setup", {"current_password": pw}),
        ("routers/auth.py", "change_password"): lambda pw: (
            "PATCH", ROUTE, {"current_password": pw, "new_password": NEW_PW}),
        ("routers/auth.py", "change_username"): lambda pw: (
            "PATCH", "/api/auth/me/username",
            {"current_password": pw, "new_username": "pcb_never"}),
        ("routers/peers.py", "add_peer"): lambda pw: (
            "POST", "/api/peers",
            {"id": "pcb-peer", "name": "never", "url": "https://peer.example.com",
             "current_password": pw}),
        ("routers/peers.py", "update_peer"): lambda pw: (
            "PATCH", "/api/peers/pcb-peer", {"enabled": False, "current_password": pw}),
        ("routers/settings.py", "update_settings"): lambda pw: (
            "PUT", "/api/settings", {"current_password": pw}),
        ("routers/users.py", "add_user"): lambda pw: (
            "POST", "/api/users",
            {"username": "pcb_never", "password": NEW_PW, "role": "member",
             "current_password": pw}),
        ("routers/users.py", "change_role"): lambda pw: (
            "PATCH", f"/api/users/{other_id}/role",
            {"role": "admin", "current_password": pw}),
        ("routers/users.py", "change_permissions"): lambda pw: (
            "PATCH", f"/api/users/{other_id}/permissions",
            {"permissions": ["chat", "manage_users"], "current_password": pw}),
        ("routers/users.py", "admin_mfa_reset"): lambda pw: (
            "POST", f"/api/admin/users/{other_id}/mfa-reset", {"current_password": pw}),
        ("routers/users.py", "admin_unlock_user"): lambda pw: (
            "POST", f"/api/admin/users/{other_id}/unlock", {"current_password": pw}),
    }


class _HelperCalls(ast.NodeVisitor):
    """Every call of require_step_up in one module, as (function it is in,
    the action phrase it passes)."""

    def __init__(self):
        self.inside = ["(module level)"]
        self.found = []

    def visit_FunctionDef(self, node):
        self.inside.append(node.name)
        self.generic_visit(node)
        self.inside.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = getattr(node.func, "id", getattr(node.func, "attr", None))
        if name == "require_step_up":
            action = node.args[2] if len(node.args) > 2 else None
            self.found.append((self.inside[-1],
                               action.value if isinstance(action, ast.Constant) else None))
        self.generic_visit(node)


def _helper_calls():
    """{(module, function): action phrase} for every caller under app/."""
    app_dir = Path(jwt_auth.__file__).resolve().parent
    calls = {}
    for path in sorted(app_dir.rglob("*.py")):
        visitor = _HelperCalls()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig")))
        for func, action in visitor.found:
            calls[(path.relative_to(app_dir).as_posix(), func)] = action
    return calls


def test_every_caller_of_the_helper_has_a_request_here():
    """The doors are whatever calls the helper, read from the source - not a
    list kept by hand. A new caller fails here until it has a request above,
    and with it a place in the two cases below."""
    assert set(_helper_calls()) == set(_doors(0)), \
        sorted(set(_helper_calls()) ^ set(_doors(0)))


def test_every_door_counts_in_the_one_count_and_is_refused_by_it(client, accounts, monkeypatch):
    """One wrong password at each door in turn, with an unlock after each so
    the lock never lands. The first LIMIT doors check it and answer that it
    is wrong; every door after them refuses without checking. Run once from
    each starting point, so each door is among the ones that count."""
    other = accounts("member")
    keys = sorted(_doors(other["id"]))
    checks = _Checks(monkeypatch)
    for start in range(0, len(keys), LIMIT):
        owner = accounts("owner")
        order = keys[start:] + keys[:start]
        seen_before = checks.count()
        for position, key in enumerate(order):
            method, path, body = _doors(other["id"])[key](NOT_IT)
            r = client.request(method, path, headers=owner["headers"], json=body)
            if position < LIMIT:
                assert r.status_code == 400 and r.json()["detail"] == WRONG, (key, r.text)
            else:
                assert _is_the_bound(r), (key, r.status_code, r.text)
            unlock_user(owner["id"])
        assert checks.count() - seen_before == LIMIT, (start, checks.seen)


def test_a_right_password_past_the_limit_is_refused_at_every_door(client, accounts, monkeypatch):
    """With the count full and the lock cleared, the RIGHT password does
    nothing at any door: each answers the count's refusal, and none of them
    hashes it."""
    other = accounts("member")
    owner = accounts("owner")
    for _ in range(LIMIT):
        _change(client, owner, NOT_IT)
    unlock_user(owner["id"])
    before = (_row(owner), _row(other))
    checks = _Checks(monkeypatch)
    for key, door in sorted(_doors(other["id"]).items()):
        method, path, body = door(PW)
        r = client.request(method, path, headers=owner["headers"], json=body)
        assert _is_the_bound(r), (key, r.status_code, r.text)
    assert checks.seen == []
    assert (_row(owner), _row(other)) == before, "a refused door changed an account"


# -- FIX: the lock is read from the stored row, and looked at once more -------

def test_a_lock_stored_after_the_session_was_read_is_seen(client, member, monkeypatch):
    """The row a route hands the helper was read by the session's dependency
    before the handler ran. A lock stored after that read used to be missed:
    the right password went through, and cleared the lock. The lock is read
    from the stored row, so nothing is hashed at all - a wrong password is
    refused by the lock like a right one. (A helper that read the session's
    copy would still refuse the right one, in the statement that clears the
    count; it would have hashed both.)"""
    before = _row(member)["password_hash"]
    checks = _Checks(monkeypatch)
    real = jwt_auth.user_from_token

    def read_then_a_lock_lands(token):
        user = real(token)
        lock_user(user["id"], _in_30_minutes())
        return user

    monkeypatch.setattr(jwt_auth, "user_from_token", read_then_a_lock_lands)
    for password in (NOT_IT, PW):
        r = _change(client, member, password)
        assert r.status_code == 429, (password, r.text)
        assert r.json()["detail"].startswith("Account locked. Try again in "), r.text
        assert checks.seen == [], "a password was hashed for a locked account"
    assert _row(member)["password_hash"] == before
    assert _row(member)["locked_until"], "the refused request cleared the lock"
    assert _row(member)["failed_attempts"] == 0, "a refused request was counted as a miss"


def test_a_lock_stored_while_the_right_password_was_being_checked_is_kept(client, member, monkeypatch):
    """The lock refuses a right password too. One that lands while a right
    password is being checked is looked at in the statement that would have
    cleared the count: the lock stays and the request is refused."""
    before = _row(member)["password_hash"]
    real = jwt_auth.verify_password

    def check_while_a_lock_lands(plain, hashed):
        answer = real(plain, hashed)
        lock_user(member["id"], _in_30_minutes())
        return answer

    monkeypatch.setattr(jwt_auth, "verify_password", check_while_a_lock_lands)
    r = _change(client, member, PW)
    assert r.status_code == 429, r.text
    assert r.json()["detail"].startswith("Account locked. Try again in "), r.text
    assert _row(member)["password_hash"] == before
    assert _row(member)["locked_until"], "a right password cleared a lock stored during its check"


# -- FIX: a password that cannot be encoded is a miss -------------------------

def test_a_password_that_cannot_be_encoded_is_a_miss_not_a_server_error(client, member,
                                                                      monkeypatch):
    """JSON can carry text that is not valid Unicode (half of a surrogate
    pair), and such text cannot be encoded for the hash. It was counted and
    then raised: a server error, with no miss recorded. It can never match a
    stored password, so it is a miss like any other - the same answer, the
    same count against the lock, and nothing handed to the hash."""
    cannot_be_encoded = "not" + chr(0xD800) + "it"
    body = json.dumps({"current_password": cannot_be_encoded, "new_password": NEW_PW})
    checks = _Checks(monkeypatch)
    answers = []
    for _ in range(LIMIT):
        try:
            r = client.patch(ROUTE, content=body, headers={
                **member["headers"], "Content-Type": "application/json"})
            answers.append((r.status_code, r.json().get("detail")))
        except Exception as exc:          # the route raised: a server error
            answers.append(("error", type(exc).__name__))
    assert answers == [(400, WRONG)] * (LIMIT - 1) + [(429, LOCKED_NOW)], answers
    assert checks.seen == [], "text that cannot be encoded was handed to the hash"
    row = _row(member)
    assert row["failed_attempts"] == LIMIT and row["locked_until"]
    assert verify_password(PW, row["password_hash"])


# -- PINS: the window, and what does not clear the count ----------------------

def _count_row(account):
    """The count's stored row: (when it expires, its document), or None."""
    from app.db import get_session
    from app.models import SecurityState
    with get_session() as db:
        row = db.get(SecurityState, f"pw_fail:{account['id']}")
        return None if row is None else (row.expires_at, json.loads(row.value))


def test_the_window_is_fixed_a_later_attempt_does_not_move_its_end(client, member):
    """The window runs from the FIRST password and ends by itself. If an
    attempt moved its end, a session could keep an account's step-up doors
    shut for as long as it kept sending - and the wait the refusal names
    would never arrive."""
    assert _change(client, member, NOT_IT).status_code == 400
    ends, first = _count_row(member)
    assert first["n"] == 1
    for _ in range(LIMIT - 1):
        _change(client, member, NOT_IT)
    unlock_user(member["id"])
    assert _count_row(member) == (ends, {**first, "n": LIMIT}), "a counted attempt moved the window"
    for _ in range(2):
        # 429 with no lock stored: the count's refusal, whatever its words.
        assert _change(client, member, NOT_IT).status_code == 429
        assert not _row(member)["locked_until"]
    assert _count_row(member) == (ends, {**first, "n": LIMIT + 2}), "a refused attempt moved the window"


def test_a_right_authenticator_code_does_not_clear_the_count(client, member):
    """A code is not proof of the password. With the count full, a right
    code at the code step-up clears the lock's count, as it always has, and
    leaves this one: the right password is still refused."""
    import pyotp
    from app.db import get_session
    from app.models import User
    seed = pyotp.random_base32()
    with get_session() as db:
        db.query(User).filter(User.id == member["id"]).update(
            {"mfa_enabled": True, "mfa_secret": seed})
    for _ in range(LIMIT):
        _change(client, member, NOT_IT)
    unlock_user(member["id"])
    full = _count_row(member)
    assert full is not None and full[1]["n"] == LIMIT
    jwt_auth.require_totp_step_up(_row(member), pyotp.TOTP(seed).now(), "re-key your authenticator")
    assert _count_row(member) == full, "a right code cleared the count of passwords"
    # Still refused, right password and all: 429 with no lock stored.
    assert _change(client, member, PW).status_code == 429
    assert not _row(member)["locked_until"]


# -- FIX: misses that overlap are each counted --------------------------------

def test_two_misses_that_overlap_are_both_counted(member):
    """The count is raised by one statement, so a second miss that lands
    between the first one's start and its write is not lost. Here the second
    is made at the moment the first is about to write."""
    from sqlalchemy import event
    from app.db import engine
    from app.users import increment_failed_attempts
    second = []

    def a_second_miss_lands(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE USERS") and not second:
            second.append(None)
            second.append(increment_failed_attempts(member["id"]))

    event.listen(engine, "before_cursor_execute", a_second_miss_lands)
    try:
        first = increment_failed_attempts(member["id"])
    finally:
        event.remove(engine, "before_cursor_execute", a_second_miss_lands)
    assert len(second) == 2, "the overlap was never made"
    assert _row(member)["failed_attempts"] == 2, "one of two overlapping misses was lost"
    assert sorted([first, second[1]]) == [1, 2], "two misses were handed the same count"


# -- FIX: an Owner's lock is an Owner's to clear ------------------------------

def test_only_an_owner_unlocks_an_owners_account(client, accounts, admin_headers):
    """The ceiling the MFA reset carries: an Admin who could clear an Owner's
    lock could hand back the guesses it stopped.

    Re-pointed in place: the unlock asks for the caller's own password
    before it looks at the ceiling (test_unlock_takes_proof.py), so each
    caller here sends theirs. The ceiling's answers are the same."""
    owner, admin = accounts("owner"), accounts("admin")
    lock_user(owner["id"], _in_30_minutes())
    r = client.post(f"/api/admin/users/{owner['id']}/unlock", headers=admin["headers"],
                    json={"current_password": PW})
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "Only an Owner can unlock an Owner's account"}
    assert _row(owner)["locked_until"], "a refused unlock cleared the lock"
    r = client.post(f"/api/admin/users/{owner['id']}/unlock", headers=admin_headers,
                    json={"current_password": "AdminPass1"})
    assert r.status_code == 200 and r.json() == {"status": "unlocked"}, r.text
    assert not _row(owner)["locked_until"]


# -- FIX: a missing field is a string 400 -------------------------------------

def test_a_missing_new_password_or_new_username_is_a_string_400(client, member):
    """A required field answers 422 with a list-shaped detail, which a client
    that renders `detail` raw cannot show. The current password is still
    asked first, so a wrong one learns nothing else."""
    r = client.patch(ROUTE, headers=member["headers"], json={"current_password": PW})
    assert r.status_code == 400 and r.json() == {"detail": "A new password is required"}, r.text
    r = client.patch("/api/auth/me/username", headers=member["headers"],
                     json={"current_password": PW})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str), r.text
    for path in (ROUTE, "/api/auth/me/username"):
        r = client.patch(path, headers=member["headers"], json={"current_password": NOT_IT})
        assert r.status_code == 400 and r.json() == {"detail": WRONG}, (path, r.text)
    row = _row(member)
    assert row["username"] == member["name"] and verify_password(PW, row["password_hash"])


# -- FIX: what an account with no usable password is told ---------------------

def test_no_usable_password_is_told_in_words_that_fit_every_action():
    """The refusal addresses the caller, so it reads right for every action
    phrase a door passes in - "you cannot change your password" - and it
    names no remedy this surface does not have."""
    from fastapi import HTTPException
    actor = {"id": 0, "username": "no-password", "password_hash": unusable_password_hash()}
    actions = sorted({action for action in _helper_calls().values()})
    assert actions and None not in actions, "a caller passes an action that is not a plain string"
    assert "change your password" in actions
    for action in actions:
        with pytest.raises(HTTPException) as refused:
            jwt_auth.require_step_up(actor, "anything", action)
        assert refused.value.status_code == 400
        assert refused.value.detail == (
            f"Your account has no usable password, so you cannot {action}. "
            f"Nothing on this instance sets a password for an account that has none.")


# -- CONTROL: one request at a time answers as it did -------------------------

def test_control_one_wrong_password_at_a_time_answers_as_before(client, member):
    for miss in range(1, LIMIT + 1):
        r = _change(client, member, NOT_IT)
        if miss < LIMIT:
            assert r.status_code == 400 and r.json() == {"detail": WRONG}, (miss, r.text)
            assert _row(member)["failed_attempts"] == miss
        else:
            assert r.status_code == 429 and r.json() == {"detail": LOCKED_NOW}, (miss, r.text)
    for password in (NOT_IT, PW):
        r = _change(client, member, password)
        assert r.status_code == 429, r.text
        assert r.json()["detail"].startswith("Account locked. Try again in "), r.text
    assert verify_password(PW, _row(member)["password_hash"])


def test_control_a_right_password_clears_the_count_and_the_next_miss_counts_from_one(client, member):
    for _ in range(LIMIT - 1):
        assert _change(client, member, NOT_IT).status_code == 400
    assert _change(client, member, PW, new_password=NEW_PW).status_code == 200
    assert _row(member)["failed_attempts"] == 0
    # Cleared, not merely under the limit: as many misses again read as
    # misses, the first of them counted as one, and the right one still works.
    for miss in range(1, LIMIT):
        r = _change(client, member, NOT_IT)
        assert r.status_code == 400 and r.json() == {"detail": WRONG}, (miss, r.text)
        assert _row(member)["failed_attempts"] == miss
    assert _change(client, member, NEW_PW, new_password=PW).status_code == 200


def test_control_a_limit_of_zero_still_lets_the_right_password_through(client, member, monkeypatch):
    """MAX_LOGIN_ATTEMPTS=0 has always meant "lock on the first miss". The
    count reads the same knob, and must not read a zero as "check nothing":
    the right password still passes, and the first miss still locks."""
    monkeypatch.setattr(jwt_auth, "MAX_LOGIN_ATTEMPTS", 0)
    assert _change(client, member, PW, new_password=NEW_PW).status_code == 200
    r = _change(client, member, NOT_IT)
    assert r.status_code == 429 and r.json() == {"detail": LOCKED_NOW}, r.text
    assert verify_password(NEW_PW, _row(member)["password_hash"])


def test_control_the_sign_in_answers_and_locks_as_before(client, member):
    for _ in range(LIMIT):
        r = _sign_in(client, member, NOT_IT)
        assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text
    assert _row(member)["locked_until"], "the sign-in's misses did not lock the account"
    r = _sign_in(client, member, PW)
    assert r.status_code == 401 and r.json() == SIGN_IN_REFUSED, r.text
    unlock_user(member["id"])
    assert _sign_in(client, member, PW).status_code == 200
    # The sign-in's misses are the lock's, not the step-up doors': after them
    # a wrong password at a step-up door is still a plain wrong password.
    for _ in range(LIMIT - 1):
        r = _change(client, member, NOT_IT)
        assert r.status_code == 400 and r.json() == {"detail": WRONG}, r.text


def test_control_an_administrators_unlock_still_lets_the_person_sign_in(client, member, admin_headers):
    # Re-pointed in place, here and in the case below: the unlock takes the
    # caller's own password (test_unlock_takes_proof.py), so it is sent.
    for _ in range(LIMIT):
        _change(client, member, NOT_IT)
    assert _row(member)["locked_until"]
    assert _sign_in(client, member, PW).status_code == 401
    r = client.post(f"/api/admin/users/{member['id']}/unlock", headers=admin_headers,
                    json={"current_password": "AdminPass1"})
    assert r.status_code == 200 and r.json() == {"status": "unlocked"}, r.text
    assert _sign_in(client, member, PW).status_code == 200


def test_control_an_admin_still_unlocks_a_members_account(client, accounts, member):
    admin = accounts("admin")
    lock_user(member["id"], _in_30_minutes())
    r = client.post(f"/api/admin/users/{member['id']}/unlock", headers=admin["headers"],
                    json={"current_password": PW})
    assert r.status_code == 200 and r.json() == {"status": "unlocked"}, r.text
    assert not _row(member)["locked_until"]
