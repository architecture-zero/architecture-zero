"""An account that does not exist is a 404.

Every route that takes a user id in its path writes by that id alone. Four of
them answered as if the write had happened for an id that names no account -
"deactivated", "updated", "MFA disabled" - and logged it as done. Each
answers 404 "No such user" now, in the place the unlock answers it: after any
proof the route asks for, so a caller without the proof learns nothing about
which ids exist, and after the caller's own permission and tier checks.
Nothing is written, cleared or logged for such an id.

A deactivated account is still an account, and what each route does for one
is pinned here too:

  - deactivate: answers "deactivated" again, as it always has;
  - department: written, as before;
  - role: 404, as before (that route reads active accounts only);
  - unlock: the lock is cleared, under the ceiling (the unlock's own file);
  - permissions and the MFA reset: written, as before - but the ceilings now
    see the row. They read the target through a reader that hides a
    deactivated row while the write did not, so an account below the top
    tier changed a deactivated top-tier account's permissions and removed
    its second factor.

FIX (each fails on the routes as they were):
  - deactivate, department, permissions and the MFA reset answer 404 for an
    id that names no account, in range or past it, and write and log nothing
  - the permissions write that raises authority and the MFA reset answer it
    only after the caller's own password
  - every route with {user_id} in its path, found from the app itself,
    answers 404 for an id that names no account
  - the permissions ceiling and the MFA reset's ceiling hold for a
    deactivated account
  - every function with {user_id} in its route's path raises a 404 (read
    from the source)
CONTROL (true before and after, by design):
  - the caller's own checks still come first: no manage_users, and the
    department change below the top tier
  - each route still does its work for an account that exists
  - what each route does for a deactivated account, where that has not
    changed
  - the role change and the unlock answer 404 as they did

Every case drives the app's own routes through the suite's test client, on
throwaway accounts of its own.
"""
import ast
import itertools
import json
from pathlib import Path

import pyotp
import pytest

from app import jwt_auth
from app.jwt_auth import create_access_token, hash_password
from app.routers import users as users_router
from app.users import (create_user, deactivate_user, enable_mfa,
                       get_user_by_id_any_status, set_mfa_secret, unlock_user)

PW = "OldPassw0rd!x"
NOT_IT = "not-the-password"
NO_SUCH_USER = {"detail": "No such user"}
WRONG_PW = {"detail": "Current password is incorrect"}
PAST_THE_RANGE = 2 ** 70

_names = itertools.count(1)


def _account(role, mfa=False):
    """A throwaway account with a known password and a session of its own."""
    name = f"m404_{next(_names)}"
    uid = create_user(name, hash_password(PW), role=role)
    if mfa:
        set_mfa_secret(uid, pyotp.random_base32())
        enable_mfa(uid)
    token = create_access_token(uid, name, role)
    return {"id": uid, "name": name, "role": role,
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


def _send(client, method, path, headers, body=None):
    """One request. Returns (status, body), or ("error", name) if the route
    raised."""
    try:
        if body is None:
            r = client.request(method, path, headers=headers)
        else:
            r = client.request(method, path, content=json.dumps(body),
                               headers={**headers, "Content-Type": "application/json"})
        return r.status_code, r.json()
    except Exception as exc:
        return "error", type(exc).__name__


def _nobody():
    """An id inside the table's range that names no account."""
    from app.db import get_session
    from app.models import User
    with get_session() as db:
        newest = db.query(User.id).order_by(User.id.desc()).first()[0]
    return newest + 1_000_000


def _row(account):
    return get_user_by_id_any_status(account["id"])


class _Done:
    """Records what the users router wrote and logged while it is installed:
    every writer the router imported from app.users, the two clears it asks
    of app.security, and its log lines."""

    WRITERS = ("deactivate_user", "update_user_role", "update_user_department",
               "update_user_permissions", "revoke_all_user_tokens", "disable_mfa",
               "unlock_user", "update_user_password", "reactivate_user")

    def __init__(self, monkeypatch):
        from app import security
        self.calls = []
        for name in self.WRITERS:
            if hasattr(users_router, name):
                monkeypatch.setattr(users_router, name, self._recorder(name, getattr(users_router, name)))
        for name in ("clear_totp_attempts", "clear_mfa_pending"):
            if hasattr(security, name):
                monkeypatch.setattr(security, name, self._recorder(name, getattr(security, name)))
        monkeypatch.setattr(users_router, "log", self._recorder("log", users_router.log))

    def _recorder(self, name, real):
        def call(*args, **kwargs):
            self.calls.append((name,) + args[:1])
            return real(*args, **kwargs)
        return call


# -- FIX: the four routes ------------------------------------------------------

@pytest.mark.parametrize("which", ["in range", "past the range"])
def test_deactivating_an_id_that_names_no_account_is_a_404(client, accounts, monkeypatch, which):
    """It answered "deactivated", revoked that id's sessions and logged a
    deactivation. It is a 404 and nothing is done."""
    top = accounts("owner")
    user_id = _nobody() if which == "in range" else PAST_THE_RANGE
    done = _Done(monkeypatch)
    answer = _send(client, "DELETE", f"/api/users/{user_id}", top["headers"])
    assert answer == (404, NO_SUCH_USER), answer
    assert done.calls == [], done.calls


@pytest.mark.parametrize("which", ["in range", "past the range"])
def test_a_department_for_an_id_that_names_no_account_is_a_404(client, accounts, monkeypatch, which):
    top = accounts("owner")
    user_id = _nobody() if which == "in range" else PAST_THE_RANGE
    done = _Done(monkeypatch)
    answer = _send(client, "PATCH", f"/api/users/{user_id}/department", top["headers"],
                   {"department": "general"})
    assert answer == (404, NO_SUCH_USER), answer
    assert done.calls == [], done.calls


@pytest.mark.parametrize("which", ["in range", "past the range"])
@pytest.mark.parametrize("permissions", [["chat"], [], None],
                         ids=["a list", "an empty list", "a reset"])
def test_permissions_for_an_id_that_names_no_account_are_a_404(client, accounts, monkeypatch,
                                                              permissions, which):
    """A write that raises nobody's authority asks for no proof, so the 404
    is the first answer. It answered "updated"."""
    top = accounts("owner")
    user_id = _nobody() if which == "in range" else PAST_THE_RANGE
    done = _Done(monkeypatch)
    answer = _send(client, "PATCH", f"/api/users/{user_id}/permissions", top["headers"],
                   {"permissions": permissions})
    assert answer == (404, NO_SUCH_USER), answer
    assert done.calls == [], done.calls


@pytest.mark.parametrize("which", ["in range", "past the range"])
def test_a_grant_for_an_id_that_names_no_account_is_a_404_after_the_proof(client, accounts,
                                                                         monkeypatch, which):
    """A write that would hand out manage_users asks for the caller's own
    password first. Without it, and with a wrong one, the answer is the
    step-up's - the same for an id that exists and one that does not - and
    only the right password is told that the id names no account."""
    top = accounts("owner")
    user_id = _nobody() if which == "in range" else PAST_THE_RANGE
    path = f"/api/users/{user_id}/permissions"
    grant = {"permissions": ["chat", "manage_users"]}
    answer = _send(client, "PATCH", path, top["headers"], grant)
    assert answer == (400, {"detail": "Your current password is required to grant "
                                      "manage_users or manage_system"}), answer
    answer = _send(client, "PATCH", path, top["headers"], {**grant, "current_password": NOT_IT})
    assert answer == (400, WRONG_PW), answer
    done = _Done(monkeypatch)
    answer = _send(client, "PATCH", path, top["headers"], {**grant, "current_password": PW})
    assert answer == (404, NO_SUCH_USER), answer
    assert done.calls == [], done.calls


@pytest.mark.parametrize("which", ["in range", "past the range"])
def test_an_mfa_reset_for_an_id_that_names_no_account_is_a_404_after_the_proof(
        client, accounts, monkeypatch, which):
    """The reset asks for the caller's own password first, for every id. With
    the right one it answered "MFA disabled", cleared that id's code count
    and logged a reset. It is a 404 and nothing is cleared or logged."""
    top = accounts("owner")
    user_id = _nobody() if which == "in range" else PAST_THE_RANGE
    path = f"/api/admin/users/{user_id}/mfa-reset"
    answer = _send(client, "POST", path, top["headers"], {})
    assert answer == (400, {"detail": "Your current password is required to reset "
                                      "a second factor"}), answer
    answer = _send(client, "POST", path, top["headers"], {"current_password": NOT_IT})
    assert answer == (400, WRONG_PW), answer
    done = _Done(monkeypatch)
    answer = _send(client, "POST", path, top["headers"], {"current_password": PW})
    assert answer == (404, NO_SUCH_USER), answer
    assert done.calls == [], done.calls


# A body that passes every proof and names a value for every field a route
# with {user_id} in its path reads, on this surface or a port of it.
_PASSES = {"current_password": PW, "role": "admin", "department": "general",
           "permissions": ["chat"], "new_password": "NewPassw0rd!y",
           "username": "m404_never_made", "email": "m404-never@example.com",
           "groups": [], "external_ids": [], "enabled": False}


def _routes_with_a_user_id():
    from app.main import app
    return sorted((method, route.path) for route in app.routes
                  if "{user_id}" in getattr(route, "path", "")
                  for method in sorted(route.methods))


def test_every_route_with_a_user_id_in_its_path_answers_404_for_an_id_that_names_no_account(
        client, accounts):
    """Found from the app's own route table, so a route added later is asked
    too: as the top tier, with a body that passes its proof, an id that names
    no account - in range and past it - is a 404 in words at every one."""
    top = accounts("owner")
    routes = _routes_with_a_user_id()
    assert len(routes) >= 6, routes
    wrong = []
    for method, path in routes:
        for user_id in (_nobody(), PAST_THE_RANGE):
            answer = _send(client, method, path.replace("{user_id}", str(user_id)),
                           top["headers"], _PASSES)
            if answer != (404, NO_SUCH_USER):
                wrong.append((method, path, user_id == PAST_THE_RANGE, answer))
    assert wrong == [], wrong


def test_every_function_with_a_user_id_in_its_path_raises_a_404():
    """Read from the source: every function under app/ whose route decorator
    has {user_id} in its path raises an HTTPException with status 404. A new
    route that writes by id without asking whether the id names anyone fails
    here."""
    app_dir = Path(jwt_auth.__file__).resolve().parent
    seen, silent = 0, []
    for path in sorted(app_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            in_path = any(isinstance(d, ast.Call) and d.args
                          and isinstance(d.args[0], ast.Constant)
                          and "{user_id}" in str(d.args[0].value)
                          for d in node.decorator_list)
            if not in_path:
                continue
            seen += 1
            raises_404 = any(
                isinstance(call, ast.Call) and getattr(call.func, "id", "") == "HTTPException"
                and any(k.arg == "status_code" and isinstance(k.value, ast.Constant)
                        and k.value.value == 404 for k in call.keywords)
                for call in ast.walk(node))
            if not raises_404:
                silent.append((path.relative_to(app_dir).as_posix(), node.name))
    assert seen >= 6 and silent == [], (seen, silent)


# -- FIX: the ceilings see a deactivated row -----------------------------------

def test_the_permissions_ceiling_holds_for_a_deactivated_account(client, accounts):
    """The ceiling read the target through a reader that hides a deactivated
    row, and the write did not: an account below the top tier rewrote a
    deactivated top-tier account's permissions. The target is read whatever
    its status, so the ceiling refuses it there too; the top tier still
    writes it."""
    top, other, mid = accounts("owner"), accounts("owner"), accounts("admin")
    deactivate_user(top["id"])
    path = f"/api/users/{top['id']}/permissions"
    answer = _send(client, "PATCH", path, mid["headers"], {"permissions": ["chat"]})
    assert answer == (403, {"detail": "Only an Owner can change an Owner's permissions"}), answer
    assert _row(top)["permissions"] in ({}, []), "a refused write changed a deactivated account"
    answer = _send(client, "PATCH", path, other["headers"], {"permissions": ["chat"]})
    assert answer == (200, {"status": "updated"}), answer
    assert _row(top)["permissions"] == ["chat"]


def test_the_mfa_resets_ceiling_holds_for_a_deactivated_account(client, accounts):
    """The same for the second factor: an account below the top tier, with
    its own password, removed a deactivated top-tier account's authenticator.
    It is refused; the top tier still resets it."""
    top, other, mid = accounts("owner", mfa=True), accounts("owner"), accounts("admin")
    deactivate_user(top["id"])
    path = f"/api/admin/users/{top['id']}/mfa-reset"
    answer = _send(client, "POST", path, mid["headers"], {"current_password": PW})
    assert answer == (403, {"detail": "Only an Owner can reset an Owner's MFA"}), answer
    assert _row(top)["mfa_enabled"], "a refused reset removed a deactivated account's factor"
    answer = _send(client, "POST", path, other["headers"], {"current_password": PW})
    assert answer == (200, {"status": "MFA disabled"}), answer
    assert not _row(top)["mfa_enabled"]


# -- CONTROL -------------------------------------------------------------------

def test_control_the_callers_own_checks_come_first(client, accounts):
    """An account without manage_users is refused at every one of them
    before anything about the id is said, and the department change below
    the top tier is refused as it was - for an id that exists and one that
    does not."""
    low, mid, someone = accounts("member"), accounts("admin"), accounts("member")
    for user_id in (someone["id"], _nobody(), PAST_THE_RANGE):
        for method, path in _routes_with_a_user_id():
            answer = _send(client, method, path.replace("{user_id}", str(user_id)),
                           low["headers"], _PASSES)
            assert answer == (403, {"detail": "Permission required: manage_users"}), \
                (method, path, answer)
        answer = _send(client, "PATCH", f"/api/users/{user_id}/department", mid["headers"],
                       {"department": "general"})
        assert answer == (403, {"detail": "Only an Owner can change an account's "
                                          "department"}), answer


def test_control_each_route_still_does_its_work_for_an_account_that_exists(client, accounts):
    top, someone = accounts("owner"), accounts("member", mfa=True)
    uid = someone["id"]
    answer = _send(client, "PATCH", f"/api/users/{uid}/department", top["headers"],
                   {"department": "general"})
    assert answer == (200, {"status": "updated"}), answer
    answer = _send(client, "PATCH", f"/api/users/{uid}/permissions", top["headers"],
                   {"permissions": ["chat"]})
    assert answer == (200, {"status": "updated"}), answer
    assert _row(someone)["permissions"] == ["chat"]
    answer = _send(client, "PATCH", f"/api/users/{uid}/permissions", top["headers"],
                   {"permissions": None})
    assert answer == (200, {"status": "updated"}), answer
    answer = _send(client, "POST", f"/api/admin/users/{uid}/mfa-reset", top["headers"],
                   {"current_password": PW})
    assert answer == (200, {"status": "MFA disabled"}), answer
    assert not _row(someone)["mfa_enabled"]
    answer = _send(client, "DELETE", f"/api/users/{uid}", top["headers"])
    assert answer == (200, {"status": "deactivated"}), answer
    assert not _row(someone)["is_active"]


def test_control_a_deactivated_account_is_answered_as_it_was(client, accounts):
    """Still an account: deactivating it again answers "deactivated", its
    department and its permissions are written, its second factor is reset,
    and the role change answers 404 as it did (that route reads active
    accounts only)."""
    top, gone = accounts("owner"), accounts("member", mfa=True)
    uid = gone["id"]
    deactivate_user(uid)
    answer = _send(client, "DELETE", f"/api/users/{uid}", top["headers"])
    assert answer == (200, {"status": "deactivated"}), answer
    answer = _send(client, "PATCH", f"/api/users/{uid}/department", top["headers"],
                   {"department": "general"})
    assert answer == (200, {"status": "updated"}), answer
    answer = _send(client, "PATCH", f"/api/users/{uid}/permissions", top["headers"],
                   {"permissions": ["chat"]})
    assert answer == (200, {"status": "updated"}), answer
    assert _row(gone)["permissions"] == ["chat"]
    answer = _send(client, "POST", f"/api/admin/users/{uid}/mfa-reset", top["headers"],
                   {"current_password": PW})
    assert answer == (200, {"status": "MFA disabled"}), answer
    answer = _send(client, "PATCH", f"/api/users/{uid}/role", top["headers"],
                   {"role": "admin", "current_password": PW})
    assert answer == (404, NO_SUCH_USER), answer
    assert _row(gone)["role"] == "member" and not _row(gone)["is_active"]


def test_control_the_role_change_and_the_unlock_answer_404_as_they_did(client, accounts):
    top = accounts("owner")
    for user_id in (_nobody(), PAST_THE_RANGE):
        answer = _send(client, "PATCH", f"/api/users/{user_id}/role", top["headers"],
                       {"role": "admin", "current_password": PW})
        assert answer == (404, NO_SUCH_USER), answer
        answer = _send(client, "POST", f"/api/admin/users/{user_id}/unlock", top["headers"],
                       {"current_password": PW})
        assert answer == (404, NO_SUCH_USER), answer
