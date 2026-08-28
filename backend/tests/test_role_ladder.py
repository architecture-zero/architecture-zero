"""The clearance ladder Owner > Admin > Member > Guest.

Retrieval is gated by level; these tests pin the ROLE data + authz to the
ladder: Owner is the only superuser (full bypass), Admin manages
content/users/analytics but NOT system/ops, and an Admin cannot escalate
anyone to Owner. The conftest setup account is the OWNER, so its
`admin_headers` token is the Owner's.
"""
from app.permissions import (
    is_owner, effective_permissions,
    MEMBER_LEVEL, ADMIN_LEVEL, OWNER_LEVEL,
)


def test_is_owner_only_owner():
    assert is_owner({"role": "owner"})
    assert not is_owner({"role": "admin"})
    assert not is_owner({"role": "member"})
    assert not is_owner({"role": "guest"})
    assert not is_owner(None)


def test_presets_owner_vs_admin_split():
    owner  = effective_permissions({"role": "owner"})
    admin  = effective_permissions({"role": "admin"})
    member = effective_permissions({"role": "member"})
    guest  = effective_permissions({"role": "guest"})
    # Only the Owner tier holds system control.
    assert "manage_system" in owner
    assert "manage_system" not in admin
    # Admin manages content + people + analytics...
    for scope in ("manage_kb", "manage_users", "view_analytics"):
        assert scope in admin
    # ...Member/Guest do not.
    assert member == ["chat", "view_history"]
    assert guest == ["chat"]


def _make(client, headers, username, role):
    """Create a user of `role` via `headers` and return their auth header."""
    r = client.post("/api/users",
                    json={"username": username, "password": "LadderP1", "role": role},
                    headers=headers)
    assert r.status_code == 200, r.text
    tok = client.post("/api/auth/login",
                      json={"username": username, "password": "LadderP1"}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def test_admin_blocked_from_owner_only_system_endpoints(client, admin_headers):
    owner_headers = admin_headers  # conftest setup account is the Owner
    admin_h = _make(client, owner_headers, "ladder_admin", "admin")

    # Owner-only (require_owner) system endpoint: provider/model settings.
    assert client.get("/api/settings", headers=owner_headers).status_code == 200
    assert client.get("/api/settings", headers=admin_h).status_code == 403

    # A manage_users endpoint: Admin passes (has the scope), Member does not.
    member_h = _make(client, owner_headers, "ladder_member", "member")
    assert client.get("/api/users", headers=admin_h).status_code == 200
    assert client.get("/api/users", headers=member_h).status_code == 403


def test_admin_cannot_escalate_to_owner(client, admin_headers):
    owner_headers = admin_headers
    admin_h = _make(client, owner_headers, "esc_admin", "admin")

    # Admin holds manage_users, but minting an Owner is Owner-only.
    sneaky = client.post("/api/users",
                         json={"username": "sneaky_owner", "password": "LadderP1", "role": "owner"},
                         headers=admin_h)
    assert sneaky.status_code == 403, sneaky.text

    # Admin CAN create a Member; the Owner CAN create an Owner.
    assert client.post("/api/users",
                       json={"username": "ok_member", "password": "LadderP1", "role": "member"},
                       headers=admin_h).status_code == 200
    assert client.post("/api/users",
                       json={"username": "second_owner", "password": "LadderP1", "role": "owner"},
                       headers=owner_headers).status_code == 200


def test_invalid_role_rejected(client, admin_headers):
    r = client.post("/api/users",
                    json={"username": "bad_role", "password": "LadderP1", "role": "superuser"},
                    headers=admin_headers)
    assert r.status_code == 400


def test_admin_cannot_deactivate_owner(client, admin_headers):
    """Owner-protection. An Admin holds manage_users, but must not be able to
    deactivate the Owner - that would drop owner_exists() to false and re-open
    the public /api/auth/setup bootstrap to anyone (account-takeover chain)."""
    owner_headers = admin_headers  # conftest setup account is the Owner
    owner_id = client.get("/api/auth/me", headers=owner_headers).json()["id"]
    admin_h = _make(client, owner_headers, "deact_admin", "admin")

    r = client.delete(f"/api/users/{owner_id}", headers=admin_h)
    assert r.status_code == 403, r.text
    # Owner still active -> setup stays closed.
    assert client.post("/api/auth/setup",
                       json={"username": "x", "password": "Whatever1"}).status_code == 403


def test_setup_disabled_once_owner_exists(client):
    # conftest already ran /api/auth/setup, so an Owner exists - re-setup is refused,
    # so the public bootstrap can never mint a second superuser.
    r = client.post("/api/auth/setup", json={"username": "late", "password": "Whatever1"})
    assert r.status_code == 403


def test_the_two_guards_the_split_moved_hold_behaviourally(client, admin_headers):
    """The structural pin in test_route_authz_wiring proves the dependency is
    declared; this proves it BITES, through real tokens, for the two routes the
    system-router extraction carried by hand.

    Both were provably unguarded-in-effect at one point: downgrading them to a
    bare get_current_user left the whole suite at its exact baseline. An Admin
    is the right prober for the owner-only route and a Member for the scoped
    one, because an Admin legitimately holds view_analytics - testing that one
    with an Admin token would pass no matter what the guard said.
    """
    owner_headers = admin_headers  # conftest setup account is the Owner
    admin_h  = _make(client, owner_headers, "guard_admin", "admin")
    member_h = _make(client, owner_headers, "guard_member", "member")

    # require_owner: discloses disk usage, DB latency, which provider keys are
    # present, and fires alert webhooks inline - Admin must not reach it.
    assert client.get("/api/health/detailed", headers=owner_headers).status_code == 200
    assert client.get("/api/health/detailed", headers=admin_h).status_code == 403

    # require_permission("view_analytics"): the operator trust panel, which adds
    # provenance and working bands over the public variant.
    assert client.get("/api/admin/trust", headers=admin_h).status_code == 200
    assert client.get("/api/admin/trust", headers=member_h).status_code == 403
