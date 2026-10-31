"""Only the Owner changes an account's department (2026-10-31).

An account's department is half of what the chat reads for it (the shared
department and its own) and half of where it may write (the department write
check). PATCH /api/users/{id}/department asked for manage_users alone, so an
Admin - whose preset holds it - could move any account between departments,
its own included, and so widen its own read and write axes to a department
the Owner never opened to it.

FIX: below the Owner the route refuses, with its own sentence when the
account is the caller's own. CONTROL: the Owner changes an account's
department.
"""
import pytest

from app.users import get_user_by_id

_ADMIN = {"username": "deptchange-admin", "password": "AdminRole1"}
_MEMBER = {"username": "deptchange-member", "password": "MemberPass1"}


def _account(client, owner_headers, creds, role, department):
    client.post("/api/users", headers=owner_headers,
                json={"current_password": "AdminPass1", **creds, "role": role,
                      "department": department})   # idempotent across tests
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, r.text
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200, me.text
    return me.json()["id"], {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def accounts(client, admin_headers):
    admin_id, admin = _account(client, admin_headers, _ADMIN, "admin", "sales")
    member_id, _ = _account(client, admin_headers, _MEMBER, "member", "general")
    return admin_id, admin, member_id


def _dept(user_id):
    return get_user_by_id(user_id)["department"]


def test_an_admin_may_not_change_another_accounts_department(client, accounts):
    admin_id, admin, member_id = accounts
    before = _dept(member_id)
    r = client.patch(f"/api/users/{member_id}/department", headers=admin,
                     json={"department": "hr"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Only an Owner can change an account's department"
    assert _dept(member_id) == before


def test_an_admin_may_not_change_its_own_department(client, accounts):
    admin_id, admin, _ = accounts
    before = _dept(admin_id)
    r = client.patch(f"/api/users/{admin_id}/department", headers=admin,
                     json={"department": "hr"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Cannot change your own department"
    assert _dept(admin_id) == before


def test_the_owner_changes_an_accounts_department(client, admin_headers, accounts):
    admin_id, _, member_id = accounts
    for user_id in (member_id, admin_id):
        before = _dept(user_id)
        target = "hr" if before != "hr" else "sales"
        r = client.patch(f"/api/users/{user_id}/department", headers=admin_headers,
                         json={"department": target})
        assert r.status_code == 200, r.text
        assert _dept(user_id) == target
        client.patch(f"/api/users/{user_id}/department", headers=admin_headers,
                     json={"department": before})
