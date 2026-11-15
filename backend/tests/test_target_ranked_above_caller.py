"""Below the Owner, no account route acts on an account ranked above the caller.

Every route that changes another account refused an Owner target to anyone
but an Owner, and nothing else about the target's rank. So a Member the Owner
handed manage_users - a Member-rank account - deactivated an Admin, demoted
it to Member, narrowed its permission list, switched its second factor off
and cleared its sign-in lock. The department route was already the Owner's
alone.

FIX: below the Owner, a target whose current rank is above the caller's own
is refused with one fixed sentence, on each of those routes, after the
caller's proof and the 404 (so a caller without the proof learns nothing),
and before anything is written.
CONTROLS: the Owner acts on an Admin; an Admin acts on a Member and on
another Admin (its own rank, not above it); the self-service second-factor
reset is unchanged.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.users import (enable_mfa, get_user_by_id, get_user_by_id_any_status,
                       get_user_by_username, lock_user)

OWNER_PW = "AdminPass1"
_PW = "RankAbove1"
_ABOVE = "Cannot change an account ranked above your own"


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _login(client, username: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": _PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _create(client, admin_headers, role):
    username = _name(role)
    r = client.post("/api/users", headers=admin_headers,
                    json={"username": username, "password": _PW, "role": role,
                          "department": "general", "current_password": OWNER_PW})
    assert r.status_code == 200, r.text
    return get_user_by_username(username)["id"], username


@pytest.fixture
def member_manager(client, admin_headers):
    """A Member-rank account the Owner handed manage_users, signed in."""
    uid, username = _create(client, admin_headers, "member")
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"current_password": OWNER_PW,
                           "permissions": ["chat", "view_history", "manage_users"]})
    assert r.status_code == 200, r.text
    return uid, _login(client, username)


def _prepared_target(client, admin_headers, role):
    """A target with a second factor on and a sign-in lock in place, so the
    MFA reset and the unlock each have something to change."""
    uid, _ = _create(client, admin_headers, role)
    enable_mfa(uid)
    lock_user(uid, (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
    return uid


def _act(client, headers, pw, action, uid):
    if action == "deactivate":
        return client.request("DELETE", f"/api/users/{uid}", headers=headers)
    if action == "role":
        return client.patch(f"/api/users/{uid}/role", headers=headers,
                            json={"role": "member", "current_password": pw})
    if action == "permissions":
        return client.patch(f"/api/users/{uid}/permissions", headers=headers,
                            json={"permissions": ["chat"], "current_password": pw})
    if action == "mfa-reset":
        return client.post(f"/api/admin/users/{uid}/mfa-reset", headers=headers,
                           json={"current_password": pw})
    if action == "unlock":
        return client.post(f"/api/admin/users/{uid}/unlock", headers=headers,
                           json={"current_password": pw})
    raise AssertionError(action)


_ACTIONS = ["deactivate", "role", "permissions", "mfa-reset", "unlock"]


@pytest.mark.parametrize("action", _ACTIONS)
def test_a_member_rank_manager_cannot_act_on_an_admin(
        client, admin_headers, member_manager, action):
    _, actor = member_manager
    target = _prepared_target(client, admin_headers, "admin")
    before = get_user_by_id_any_status(target)
    r = _act(client, actor, _PW, action, target)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == _ABOVE
    after = get_user_by_id_any_status(target)
    for key in ("is_active", "role", "permissions", "mfa_enabled", "locked_until"):
        assert after[key] == before[key], key


def test_the_department_route_was_already_the_owners_alone(client, admin_headers, member_manager):
    _, actor = member_manager
    target, _ = _create(client, admin_headers, "admin")
    r = client.patch(f"/api/users/{target}/department", headers=actor,
                     json={"department": "sales"})
    assert r.status_code == 403, r.text
    assert get_user_by_id(target)["department"] == "general"


@pytest.mark.parametrize("action", _ACTIONS)
def test_control_the_owner_acts_on_an_admin(client, admin_headers, action):
    target = _prepared_target(client, admin_headers, "admin")
    r = _act(client, admin_headers, OWNER_PW, action, target)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("target_role", ["member", "admin"])
@pytest.mark.parametrize("action", _ACTIONS)
def test_control_an_admin_acts_on_its_own_rank_and_below(
        client, admin_headers, action, target_role):
    _, actor_name = _create(client, admin_headers, "admin")
    actor = _login(client, actor_name)
    target = _prepared_target(client, admin_headers, target_role)
    r = _act(client, actor, _PW, action, target)
    if action == "role" and target_role == "member":
        assert r.status_code == 200 and r.json()["status"] == "unchanged", r.text
    else:
        assert r.status_code == 200, r.text


def test_control_the_self_service_second_factor_reset_is_unchanged(client, member_manager):
    uid, actor = member_manager
    enable_mfa(uid)
    r = client.post(f"/api/admin/users/{uid}/mfa-reset", headers=actor,
                    json={"current_password": _PW})
    assert r.status_code == 200, r.text
    assert get_user_by_id(uid)["mfa_enabled"] in (False, 0)
