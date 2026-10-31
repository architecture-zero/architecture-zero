"""Below the Owner, an account is created only in the creator's own department
or the shared one, and never above the creator's own role (2026-10-31).

POST /api/users asked for manage_users alone, so a holder below the Owner
created an account in ANY department with a password of its choosing, signed
in as it and read that department through the chat - the widening the
department change door was closed against, through the create door. And the
role ceiling refused only an Owner: a Member holding manage_users created an
Admin, a rung above itself, and an Admin whose permission list the Owner had
narrowed created an Admin holding the preset it was denied.

FIX: below the Owner, another department is refused with a fixed sentence,
a role above the creator's own rung is refused, and so is a role whose preset
holds a scope the creator does not (the permission door's rule, can_grant).
CONTROL: the creator's own department (in any spelling that keys the same
collection) and the shared one are created; the Owner creates anywhere.
"""
import uuid

import pytest

from app.users import get_user_by_username

OWNER_PW = "AdminPass1"
_PW = "CreateDept1"
_OTHER_DEPT = "Only an Owner can create an account in another department"
_ABOVE = "Cannot create an account above your own role"


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _login(client, username: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": _PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _create(client, headers, actor_pw, role="member", department="general", username=None):
    username = username or _name("acct")
    r = client.post("/api/users", headers=headers,
                    json={"username": username, "password": _PW, "role": role,
                          "department": department, "current_password": actor_pw})
    return r, username


@pytest.fixture
def sales_admin(client, admin_headers):
    r, username = _create(client, admin_headers, OWNER_PW, role="admin", department="sales",
                          username=_name("sales-admin"))
    assert r.status_code == 200, r.text
    return _login(client, username)


def test_below_the_owner_another_department_is_refused(client, sales_admin):
    r, username = _create(client, sales_admin, _PW, department="hr")
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == _OTHER_DEPT
    assert get_user_by_username(username) is None


@pytest.mark.parametrize("department", ["sales", "Sales", "general"])
def test_below_the_owner_its_own_department_and_the_shared_one_are_created(
        client, sales_admin, department):
    r, username = _create(client, sales_admin, _PW, department=department)
    assert r.status_code == 200, r.text
    assert get_user_by_username(username)["department"] == department


def test_below_the_owner_a_role_above_its_own_is_refused(client, admin_headers):
    """A Member holding manage_users does not mint an Admin."""
    r, username = _create(client, admin_headers, OWNER_PW, role="member",
                          department="sales", username=_name("sales-mgr"))
    assert r.status_code == 200, r.text
    uid = get_user_by_username(username)["id"]
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"current_password": OWNER_PW,
                           "permissions": ["chat", "view_history", "manage_users"]})
    assert r.status_code == 200, r.text
    me = _login(client, username)
    r, made = _create(client, me, _PW, role="admin", department="sales")
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == _ABOVE
    assert get_user_by_username(made) is None
    # CONTROL: its own rung, in its own department.
    r, _ = _create(client, me, _PW, role="member", department="sales")
    assert r.status_code == 200, r.text


def test_below_the_owner_a_role_holding_a_scope_it_lacks_is_refused(client, admin_headers):
    """An Admin the Owner narrowed does not mint the Admin preset it was denied."""
    r, username = _create(client, admin_headers, OWNER_PW, role="admin",
                          department="sales", username=_name("narrow-admin"))
    assert r.status_code == 200, r.text
    uid = get_user_by_username(username)["id"]
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"permissions": ["chat", "view_history", "manage_users"]})
    assert r.status_code == 200, r.text
    r, made = _create(client, _login(client, username), _PW, role="admin", department="sales")
    assert r.status_code == 403, r.text
    assert r.json()["detail"].startswith("Cannot grant permissions you do not hold")
    assert get_user_by_username(made) is None


def test_the_owner_creates_in_any_department(client, admin_headers):
    for department in ("hr", "finance team"):
        r, username = _create(client, admin_headers, OWNER_PW, role="admin",
                              department=department)
        assert r.status_code == 200, r.text
        assert get_user_by_username(username)["department"] == department
