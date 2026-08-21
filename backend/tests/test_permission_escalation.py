"""The authority ceiling on permission writes.

manage_users is the permission to run people. It must not also be the
permission to become the Owner. The two axes make that easy to get wrong:
role presets withhold manage_system from admin, and change_role guards the
role axis - but effective_permissions treats a non-empty stored permission
list as an override that REPLACES the preset. Guarding one axis while the
other can override it guards nothing, and the door on the far side of that
gap is /api/admin/config, which returns provider API keys in cleartext.

These tests are the regression wall for that path. `admin_headers` in
conftest is the setup Owner; a real admin-role user is minted per test.
"""
import pytest

_ADMIN_USER = {"username": "permesc_admin", "password": "PermEsc1", "role": "admin"}


def _ensure_user(client, admin_headers, username, password, role):
    """Create the account if this module has not already made it.

    The client fixture is session-scoped, so the database persists across
    tests in this file - a blind create would hit the UNIQUE constraint on the
    second test that asks for the same account.
    """
    r = client.post("/api/users", json={"username": username, "password": password,
                                        "role": role}, headers=admin_headers)
    assert r.status_code in (200, 201, 409), r.text
    users = client.get("/api/users", headers=admin_headers).json()
    rows = users if isinstance(users, list) else users.get("users", [])
    return next(u["id"] for u in rows if u["username"] == username)


@pytest.fixture
def admin_role(client, admin_headers):
    """A genuine admin-role account (not the Owner) plus its auth headers."""
    uid = _ensure_user(client, admin_headers, _ADMIN_USER["username"],
                       _ADMIN_USER["password"], "admin")
    r = client.post("/api/auth/login", json={"username": _ADMIN_USER["username"],
                                             "password": _ADMIN_USER["password"]})
    assert r.status_code == 200, f"admin login failed: {r.text}"
    return {"id": uid, "headers": {"Authorization": f"Bearer {r.json()['access_token']}"}}


@pytest.fixture
def owner_id(client, admin_headers):
    users = client.get("/api/users", headers=admin_headers).json()
    rows = users if isinstance(users, list) else users.get("users", [])
    return next(u["id"] for u in rows if u.get("role") == "owner")


# -- The escalation itself ----------------------------------------------------

def test_admin_cannot_grant_itself_manage_system(client, admin_role):
    """The exact reported chain, step one."""
    r = client.patch(f"/api/users/{admin_role['id']}/permissions",
                     json={"permissions": ["chat", "view_history", "manage_users",
                                           "manage_system"]},
                     headers=admin_role["headers"])
    assert r.status_code == 403, r.text


def test_admin_cannot_reach_config_after_attempting_escalation(client, admin_role):
    """Step two must stay shut even after step one is attempted.

    The point of the finding was never the PATCH - it was the provider keys
    behind /api/admin/config. This asserts the door, not just the handle.
    """
    client.patch(f"/api/users/{admin_role['id']}/permissions",
                 json={"permissions": ["manage_system"]},
                 headers=admin_role["headers"])
    assert client.get("/api/admin/config", headers=admin_role["headers"]).status_code == 403


def test_admin_cannot_grant_manage_system_to_a_third_party(client, admin_role, admin_headers):
    """Granting it to a confederate is the same escalation with one more step."""
    patsy = _ensure_user(client, admin_headers, "permesc_patsy", "PermPatsy1", "member")
    r = client.patch(f"/api/users/{patsy}/permissions",
                     json={"permissions": ["chat", "manage_system"]},
                     headers=admin_role["headers"])
    assert r.status_code == 403, r.text


def test_admin_cannot_grant_view_audit_log(client, admin_role):
    """manage_system is not the only scope outside the admin preset."""
    r = client.patch(f"/api/users/{admin_role['id']}/permissions",
                     json={"permissions": ["chat", "view_audit_log"]},
                     headers=admin_role["headers"])
    assert r.status_code == 403, r.text


def test_admin_cannot_touch_an_owners_permissions(client, admin_role, owner_id):
    """Mirrors change_role: an Owner's authority is Owner-managed only.

    Refused even though every scope named here is one the admin holds - the
    target's role is the reason, not the payload.
    """
    r = client.patch(f"/api/users/{owner_id}/permissions",
                     json={"permissions": ["chat"]},
                     headers=admin_role["headers"])
    assert r.status_code == 403, r.text


def test_admin_cannot_reset_an_owner_to_defaults(client, admin_role, owner_id):
    """Reset-to-defaults is a write to the target's authority like any other.

    permissions=None takes a different branch in the endpoint; it needs its own
    assertion or the guard can be bypassed by omitting the field.
    """
    r = client.patch(f"/api/users/{owner_id}/permissions", json={},
                     headers=admin_role["headers"])
    assert r.status_code == 403, r.text


# -- What must still work -----------------------------------------------------

def test_admin_can_still_grant_what_it_holds(client, admin_role, admin_headers):
    """The ceiling is 'not above your own authority', not 'no delegation'."""
    target = _ensure_user(client, admin_headers, "permesc_delegate", "PermDeleg1", "member")
    r = client.patch(f"/api/users/{target}/permissions",
                     json={"permissions": ["chat", "view_history", "manage_kb"]},
                     headers=admin_role["headers"])
    assert r.status_code == 200, r.text


def test_owner_can_grant_manage_system(client, admin_headers):
    """The Owner is the principal the whole ceiling defers to."""
    target = _ensure_user(client, admin_headers, "permesc_promoted", "PermProm1", "admin")
    r = client.patch(f"/api/users/{target}/permissions",
                     json={"permissions": ["chat", "manage_system"]},
                     headers=admin_headers)
    assert r.status_code == 200, r.text


def test_unknown_scope_still_400s_not_403(client, admin_headers):
    """Validation order: a typo is a bad request, not a privilege refusal.

    Worth pinning - if the ceiling ran first it would answer 403 for a
    misspelled scope and send the operator hunting a permissions problem they
    do not have.
    """
    users = client.get("/api/users", headers=admin_headers).json()
    rows = users if isinstance(users, list) else users.get("users", [])
    owner = next(u["id"] for u in rows if u.get("role") == "owner")
    r = client.patch(f"/api/users/{owner}/permissions",
                     json={"permissions": ["chat", "manage_evrything"]},
                     headers=admin_headers)
    assert r.status_code == 400, r.text


# -- The policy function directly ---------------------------------------------

def test_can_grant_unit():
    from app.permissions import can_grant
    owner  = {"role": "owner"}
    admin  = {"role": "admin"}
    member = {"role": "member"}

    assert can_grant(owner, admin, ["manage_system"]) is None
    assert can_grant(owner, owner, ["manage_system"]) is None
    assert can_grant(admin, member, ["manage_system"]) is not None
    assert can_grant(admin, admin,  ["manage_system"]) is not None
    assert can_grant(admin, owner,  ["chat"]) is not None
    assert can_grant(admin, member, ["chat", "manage_kb"]) is None
    # A stored override is the actor's real authority, preset or not.
    assert can_grant({"role": "member", "permissions": ["chat", "manage_users"]},
                     {"role": "member"}, ["manage_system"]) is not None
