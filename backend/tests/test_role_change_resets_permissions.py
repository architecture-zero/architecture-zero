"""A role change resets an explicit permission list to the new role's preset (2026-09-22).

effective_permissions treats a non-empty stored list as an override that
REPLACES the role preset, and change_role wrote the role column only - so a
list survived a role change. An account given an explicit list and then
demoted kept manage_users; an account with a narrow list and then promoted
never received its new role's preset. The role change now stores "{}" (the
new role's preset) in the same write, and the answer says whether a list was
reset. Found upstream by the review of a change that added a scope in no
role preset, where an explicit list is the only way to give an Admin an
extra scope.
"""
from app.permissions import ROLE_PERMISSIONS, effective_permissions
from app.users import get_user_by_id

HIGH, LOW = "admin", "member"   # this surface's role names
_OWNER_PW = "AdminPass1"
_U = {"username": "rolereset_user", "password": "RoleReset1"}


def _uid(client, admin_headers):
    client.post("/api/users", json={"current_password": _OWNER_PW, **_U, "role": LOW},
                headers=admin_headers)
    rows = client.get("/api/users", headers=admin_headers).json()
    rows = rows if isinstance(rows, list) else rows.get("users", [])
    return next(u["id"] for u in rows if u["username"] == _U["username"])


def _perms(uid):
    return set(effective_permissions(get_user_by_id(uid)))


def _set_role(client, admin_headers, uid, role):
    r = client.patch(f"/api/users/{uid}/role", json={"current_password": _OWNER_PW, "role": role},
                     headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


def _set_list(client, admin_headers, uid, perms):
    r = client.patch(f"/api/users/{uid}/permissions",
                     json={"current_password": _OWNER_PW, "permissions": perms}, headers=admin_headers)
    assert r.status_code == 200, r.text


def test_a_demotion_takes_back_an_explicit_list(client, admin_headers):
    uid = _uid(client, admin_headers)
    _set_role(client, admin_headers, uid, HIGH)
    _set_list(client, admin_headers, uid, list(ROLE_PERMISSIONS[HIGH]))
    assert "manage_users" in _perms(uid)
    body = _set_role(client, admin_headers, uid, LOW)
    assert _perms(uid) == set(ROLE_PERMISSIONS[LOW])
    assert body["permissions_reset"] is True


def test_a_promotion_receives_the_new_preset(client, admin_headers):
    uid = _uid(client, admin_headers)
    _set_role(client, admin_headers, uid, LOW)
    _set_list(client, admin_headers, uid, ["chat"])
    assert _perms(uid) == {"chat"}
    body = _set_role(client, admin_headers, uid, HIGH)
    assert _perms(uid) == set(ROLE_PERMISSIONS[HIGH])
    assert body["permissions_reset"] is True
    _set_role(client, admin_headers, uid, LOW)


def test_a_role_change_with_no_list_reports_no_reset(client, admin_headers):
    uid = _uid(client, admin_headers)
    _set_role(client, admin_headers, uid, LOW)   # clears any list a test above left
    body = _set_role(client, admin_headers, uid, HIGH)
    assert body["permissions_reset"] is False
    _set_role(client, admin_headers, uid, LOW)
