"""The role change door holds the ceiling the create door holds.

POST /api/users refuses, below the Owner, a role above the creator's own rung
and a role whose preset holds a scope the creator does not. PATCH
/api/users/{id}/role did not: a Member holding manage_users could not CREATE
an Admin, but it could create a Member it controlled and then change that
account's role to Admin - whose preset grants manage_kb and view_analytics,
two scopes the actor never held. A role change resets the account to the new
role's preset, so the preset is what the account then holds.

FIX: below the Owner, the role change is refused for a role above the
caller's own rung, and for a role whose preset holds a scope the caller does
not (can_grant on the preset) - the same check the create door runs.
CONTROLS: a caller whose own scopes cover the preset still changes a role
(a narrowed account reset to its preset; a demotion), and the Owner still
promotes to Admin.
"""
import uuid

import pytest

from app.permissions import ROLE_PERMISSIONS, effective_permissions
from app.users import get_user_by_id, get_user_by_username

OWNER_PW = "AdminPass1"
_PW = "RoleCeiling1"


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _login(client, username: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": _PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _create(client, headers, actor_pw, role="member", department="sales"):
    username = _name("acct")
    r = client.post("/api/users", headers=headers,
                    json={"username": username, "password": _PW, "role": role,
                          "department": department, "current_password": actor_pw})
    assert r.status_code == 200, r.text
    return get_user_by_username(username)["id"], username


def _set_perms(client, admin_headers, uid, perms):
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"current_password": OWNER_PW, "permissions": perms})
    assert r.status_code == 200, r.text


def _change_role(client, headers, uid, role, pw=_PW):
    return client.patch(f"/api/users/{uid}/role", headers=headers,
                        json={"role": role, "current_password": pw})


@pytest.fixture
def member_manager(client, admin_headers):
    """A Member the Owner handed manage_users, signed in."""
    uid, username = _create(client, admin_headers, OWNER_PW, role="member")
    _set_perms(client, admin_headers, uid, ["chat", "view_history", "manage_users"])
    return uid, _login(client, username)


def test_a_member_with_manage_users_cannot_promote_its_own_account_to_admin(
        client, member_manager):
    actor_id, actor = member_manager
    # Its own creation of an Admin is refused (the create door's ceiling).
    r = client.post("/api/users", headers=actor,
                    json={"username": _name("refused"), "password": _PW, "role": "admin",
                          "department": "sales", "current_password": _PW})
    assert r.status_code == 403, r.text
    # So it creates a Member it controls, and the role door must refuse too.
    target_id, _ = _create(client, actor, _PW, role="member")
    before = get_user_by_id(target_id)
    r = _change_role(client, actor, target_id, "admin")
    assert r.status_code == 403, r.text
    after = get_user_by_id(target_id)
    assert after["role"] == before["role"] == "member"
    assert after.get("permissions") == before.get("permissions")
    held = set(effective_permissions(get_user_by_id(actor_id)))
    assert set(effective_permissions(after)) <= held


def test_a_narrowed_admin_cannot_hand_out_the_admin_preset_it_was_denied(
        client, admin_headers):
    actor_id, actor_name = _create(client, admin_headers, OWNER_PW, role="admin")
    _set_perms(client, admin_headers, actor_id, ["chat", "view_history", "manage_users"])
    actor = _login(client, actor_name)
    target_id, _ = _create(client, admin_headers, OWNER_PW, role="member")
    r = _change_role(client, actor, target_id, "admin")
    assert r.status_code == 403, r.text
    assert r.json()["detail"].startswith("Cannot grant permissions you do not hold")
    assert get_user_by_id(target_id)["role"] == "member"


def test_control_a_caller_whose_scopes_cover_the_preset_still_changes_roles(
        client, admin_headers):
    """An Admin holding its whole preset resets a narrowed Member to the
    Member preset by role, promotes it to Admin, and demotes it again."""
    actor_id, actor_name = _create(client, admin_headers, OWNER_PW, role="admin")
    actor = _login(client, actor_name)
    target_id, _ = _create(client, admin_headers, OWNER_PW, role="member")
    _set_perms(client, admin_headers, target_id, ["chat"])
    r = _change_role(client, actor, target_id, "admin")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "updated", "permissions_reset": True}
    after = get_user_by_id(target_id)
    assert after["role"] == "admin"
    assert set(effective_permissions(after)) == set(ROLE_PERMISSIONS["admin"])
    r = _change_role(client, actor, target_id, "member")
    assert r.status_code == 200, r.text
    assert get_user_by_id(target_id)["role"] == "member"


def test_control_a_member_manager_may_set_its_own_rung(client, member_manager, admin_headers):
    """The Member preset is within a Member manager's scopes and on its own
    rung, so setting it is not refused: an account already a Member answers
    unchanged."""
    _, actor = member_manager
    target_id, _ = _create(client, admin_headers, OWNER_PW, role="member")
    r = _change_role(client, actor, target_id, "member")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unchanged"


def test_control_the_owner_promotes_to_admin(client, admin_headers):
    target_id, _ = _create(client, admin_headers, OWNER_PW, role="member")
    r = _change_role(client, admin_headers, target_id, "admin", pw=OWNER_PW)
    assert r.status_code == 200, r.text
    assert get_user_by_id(target_id)["role"] == "admin"
