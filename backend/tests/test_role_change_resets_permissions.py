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
    _set_role(client, admin_headers, uid, HIGH)  # a real change clears any list a
    _set_role(client, admin_headers, uid, LOW)   # test above left (a same-role one does not)
    body = _set_role(client, admin_headers, uid, HIGH)
    assert body["permissions_reset"] is False
    _set_role(client, admin_headers, uid, LOW)


# -- The 09-22 review's residue, closed 2026-10-01 ----------------------------
# A same-role PATCH wiped the explicit list it was not changing (all five
# surfaces); the route lacked the refusal of a change to your own role and
# answered "updated" for an id naming no active account, writing the role onto a
# deactivated one; and the apex guards had no behavioral test here.


def _role_any_status(uid):
    from app.db import get_session
    from app.models import User
    with get_session() as db:
        return db.query(User).filter(User.id == uid).first().role


def test_setting_the_role_an_account_already_has_keeps_its_list(client, admin_headers):
    uid = _uid(client, admin_headers)
    _set_role(client, admin_headers, uid, HIGH)
    _set_role(client, admin_headers, uid, LOW)       # LOW, no list
    _set_list(client, admin_headers, uid, ["chat"])
    body = _set_role(client, admin_headers, uid, LOW)
    assert body == {"status": "unchanged", "permissions_reset": False}
    assert _perms(uid) == {"chat"}                 # the list it was not changing, kept
    _set_role(client, admin_headers, uid, HIGH)      # a real change still resets it
    assert _perms(uid) == set(ROLE_PERMISSIONS[HIGH])
    _set_role(client, admin_headers, uid, LOW)


def test_your_own_role_is_not_yours_to_change(client, admin_headers):
    me = client.get("/api/auth/me", headers=admin_headers).json()
    r = client.patch(f"/api/users/{me['id']}/role", headers=admin_headers,
                     json={"current_password": _OWNER_PW, "role": LOW})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Cannot change your own role"
    assert get_user_by_id(me["id"])["role"] == me["role"]


def test_a_missing_or_deactivated_account_is_a_404_and_nothing_is_written(client,
                                                                         admin_headers):
    from app.jwt_auth import hash_password
    from app.users import create_user, deactivate_user
    r = client.patch("/api/users/987654321/role", headers=admin_headers,
                     json={"current_password": _OWNER_PW, "role": LOW})
    assert r.status_code == 404, r.text
    gone = create_user("rolereset_gone", hash_password("RoleGone-2026!x"), role=LOW)
    deactivate_user(gone)
    r = client.patch(f"/api/users/{gone}/role", headers=admin_headers,
                     json={"current_password": _OWNER_PW, "role": HIGH})
    assert r.status_code == 404, r.text
    assert _role_any_status(gone) == LOW                # nothing written


def test_an_admin_can_neither_grant_owner_nor_touch_an_owner(client, admin_headers):
    """The apex guards, behaviorally: an Admin holding manage_users changes a
    Member's role, but cannot grant Owner and cannot change an Owner's role.
    The Admin made here is deactivated again - an extra active operator in the
    suite's shared database turns other files' sole-operator tests red."""
    from app.jwt_auth import hash_password
    from app.users import create_user, deactivate_user
    admin_pw = "RoleAdmin-2026!x"
    admin_id = create_user("rolereset_admin", hash_password(admin_pw), role="admin")
    try:
        tok = client.post("/api/auth/login",
                          json={"username": "rolereset_admin", "password": admin_pw})
        assert tok.status_code == 200, tok.text
        as_admin = {"Authorization": f"Bearer {tok.json()['access_token']}"}
        uid = _uid(client, admin_headers)
        owner = client.get("/api/auth/me", headers=admin_headers).json()
        grant = client.patch(f"/api/users/{uid}/role", headers=as_admin,
                             json={"current_password": admin_pw, "role": "owner"})
        assert grant.status_code == 403, grant.text
        touch = client.patch(f"/api/users/{owner['id']}/role", headers=as_admin,
                             json={"current_password": admin_pw, "role": LOW})
        assert touch.status_code == 403, touch.text
        assert get_user_by_id(uid)["role"] == LOW
        assert get_user_by_id(owner["id"])["role"] == "owner"
        ok = client.patch(f"/api/users/{uid}/role", headers=as_admin,
                          json={"current_password": admin_pw, "role": HIGH})
        assert ok.status_code == 200, ok.text          # its ordinary authority still works
        _set_role(client, admin_headers, uid, LOW)
    finally:
        deactivate_user(admin_id)
