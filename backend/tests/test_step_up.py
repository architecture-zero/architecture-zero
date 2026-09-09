"""The password step-up on every door to durable authority.

Ruled 2026-09-06 ("ask for the password again when creating accounts or
changing roles"), widened 2026-09-09 to every door the attack pass found, and
applied fleet-wide the same day - the public template first. The design
was attacked under three lenses before it shipped; this file pins what
survived.

The threat is a stolen session on an unlocked device. A bearer token proves
possession of a browser; the password proves the person. So each write that
creates or raises durable authority costs the CALLER's own password, verified
against their stored hash, refused with a 400 whose detail is a STRING (never
401 - the client evicts on it - and never 422 - a list detail unmounts the
admin panel). Failures count against the same lockout login uses.

This template's doors: create, role change, raising permission writes, and
the peer registry (a registered URL receives PEER_API_KEY).
"""
import pytest
from fastapi import HTTPException

from app.jwt_auth import (MAX_LOGIN_ATTEMPTS, hash_password, has_usable_password,
                          require_step_up, unusable_password_hash, verify_password)
from app.users import (create_user, get_user_by_id, get_user_by_username,
                       reset_failed_attempts, unlock_user)

OWNER_PW = "AdminPass1"          # conftest's Owner


@pytest.fixture(autouse=True)
def _keep_the_owner_unlocked():
    """A wrong step-up counts against the actor. The Owner is shared by the
    whole session-scoped suite, so its counter is cleared after every test
    here - the lockout itself is proven on a throwaway account below."""
    yield
    owner = get_user_by_username("testadmin")
    if owner:
        unlock_user(owner["id"])
        reset_failed_attempts(owner["id"])


def _mk(client, admin_headers, name, role="member", password="StepUpP1"):
    r = client.post("/api/users", headers=admin_headers,
                    json={"username": name, "password": password, "role": role,
                          "current_password": OWNER_PW})
    assert r.status_code in (200, 409), r.text
    return get_user_by_username(name)["id"]


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── The contract: a string 400, never 401 or 422 ─────────────────────────────

def test_create_needs_the_callers_password_and_answers_a_string_400(client, admin_headers):
    body = {"username": "su_nopw", "password": "StepUpP1", "role": "member"}
    r = client.post("/api/users", headers=admin_headers, json=body)
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], str)
    assert "password" in r.json()["detail"].lower()
    assert get_user_by_username("su_nopw") is None, "the refused create happened anyway"

    r = client.post("/api/users", headers=admin_headers,
                    json={**body, "current_password": "not-the-password"})
    assert r.status_code == 400 and "incorrect" in r.json()["detail"].lower()
    assert get_user_by_username("su_nopw") is None

    r = client.post("/api/users", headers=admin_headers,
                    json={**body, "current_password": OWNER_PW})
    assert r.status_code == 200, r.text


def test_the_step_up_is_checked_before_the_ceilings(client, admin_headers):
    """Re-authentication precedes authorization: a wrong password learns
    nothing about the role ceilings (the 403s) or the password policy."""
    r = client.post("/api/users", headers=admin_headers,
                    json={"username": "su_order", "password": "short", "role": "superuser"})
    assert r.status_code == 400
    assert "current password" in r.json()["detail"].lower()


# ── Role change ───────────────────────────────────────────────────────────────

def test_role_change_needs_the_callers_password(client, admin_headers):
    uid = _mk(client, admin_headers, "su_role")
    r = client.patch(f"/api/users/{uid}/role", headers=admin_headers, json={"role": "admin"})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)
    assert get_user_by_id(uid)["role"] == "member", "the refused role change happened anyway"
    r = client.patch(f"/api/users/{uid}/role", headers=admin_headers,
                     json={"role": "admin", "current_password": OWNER_PW})
    assert r.status_code == 200, r.text
    assert get_user_by_id(uid)["role"] == "admin"


# ── Permissions: only the writes that RAISE authority cost the password ──────

def test_permission_writes_cost_the_password_only_when_they_add_authority(client, admin_headers):
    uid = _mk(client, admin_headers, "su_perm")
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"permissions": ["chat", "view_history", "manage_kb"]})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"permissions": ["chat", "manage_users"]})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)
    assert "manage_users" not in get_user_by_id(uid)["permissions"], \
        "the refused grant happened anyway"
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"permissions": ["chat", "manage_users"],
                           "current_password": OWNER_PW})
    assert r.status_code == 200, r.text
    assert "manage_users" in get_user_by_id(uid)["permissions"]
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"permissions": ["chat"]})
    assert r.status_code == 200, r.text


def test_reset_to_defaults_costs_the_password_when_the_preset_adds_authority(client, admin_headers):
    """An admin narrowed to ["chat"] and then reset to its preset regains
    manage_users - the reset IS a raising write."""
    uid = _mk(client, admin_headers, "su_perm_admin", role="admin")
    assert client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                        json={"permissions": ["chat"]}).status_code == 200
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers, json={})
    assert r.status_code == 400, r.text
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"current_password": OWNER_PW})
    assert r.status_code == 200, r.text
    mid = _mk(client, admin_headers, "su_perm_member")
    assert client.patch(f"/api/users/{mid}/permissions", headers=admin_headers,
                        json={}).status_code == 200


# ── The peer registry: a URL that will receive PEER_API_KEY ──────────────────

def test_peer_writes_need_the_callers_password(client, admin_headers):
    peer = {"id": "su-peer", "name": "Step-up peer", "url": "https://peer.example.com"}
    r = client.post("/api/peers", headers=admin_headers, json=peer)
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)
    assert "password" in r.json()["detail"].lower()
    ids = [p["id"] for p in client.get("/api/peers", headers=admin_headers).json()["peers"]]
    assert "su-peer" not in ids, "the refused peer was registered anyway"
    r = client.patch("/api/peers/su-peer", headers=admin_headers, json={"enabled": False})
    assert r.status_code == 400 and "password" in r.json()["detail"].lower()


# ── Failures count against the login lockout - not a second oracle ───────────

def test_wrong_step_ups_lock_the_account_like_wrong_logins(client, admin_headers):
    uid = _mk(client, admin_headers, "su_locker", role="admin", password="LockerP1")
    reset_failed_attempts(uid)
    unlock_user(uid)
    me = _login(client, "su_locker", "LockerP1")
    body = {"username": "su_never", "password": "StepUpP1", "role": "member",
            "current_password": "wrong-every-time"}
    try:
        for i in range(1, MAX_LOGIN_ATTEMPTS + 1):
            r = client.post("/api/users", headers=me, json=body)
            if i < MAX_LOGIN_ATTEMPTS:
                assert r.status_code == 400, (i, r.text)
            else:
                assert r.status_code == 429, (i, r.text)
        assert get_user_by_id(uid).get("locked_until"), "no lock was written"
        # The lock holds on both doors, and the right password does not lift it.
        assert client.post("/api/auth/login", json={"username": "su_locker",
                                                    "password": "LockerP1"}).status_code == 429
        assert client.post("/api/users", headers=me,
                           json={**body, "current_password": "LockerP1"}).status_code == 429
        assert get_user_by_username("su_never") is None
    finally:
        unlock_user(uid)
        reset_failed_attempts(uid)


# ── No usable password: the sentinel, enforced ───────────────────────────────

def test_the_unusable_sentinel_never_verifies_and_never_raises():
    sentinel = unusable_password_hash("no-password")
    assert sentinel.startswith("!no-password:")
    assert not has_usable_password(sentinel)
    assert not has_usable_password({"password_hash": sentinel})
    assert has_usable_password({"password_hash": hash_password("RealPass1")})
    # bcrypt would raise "Invalid salt" on the sentinel; the guard answers False.
    assert verify_password("anything", sentinel) is False
    assert verify_password("", sentinel) is False


def test_an_account_without_a_usable_password_cannot_be_handed_authority(client, admin_headers):
    uid = create_user("su_no_pw", unusable_password_hash(), role="member")
    r = client.post("/api/auth/login", json={"username": "su_no_pw", "password": "anything"})
    assert r.status_code == 401, r.text          # a clean refusal, not a bcrypt 500
    r = client.patch(f"/api/users/{uid}/role", headers=admin_headers,
                     json={"role": "admin", "current_password": OWNER_PW})
    assert r.status_code == 400 and "usable password" in r.json()["detail"]
    assert get_user_by_id(uid)["role"] == "member"
    r = client.patch(f"/api/users/{uid}/permissions", headers=admin_headers,
                     json={"permissions": ["chat", "manage_system"], "current_password": OWNER_PW})
    assert r.status_code == 400 and "usable password" in r.json()["detail"]
    # A role with no authority in it is fine.
    r = client.patch(f"/api/users/{uid}/role", headers=admin_headers,
                     json={"role": "guest", "current_password": OWNER_PW})
    assert r.status_code in (200, 400), r.text   # guest is not a creatable role on every build


def test_an_actor_without_a_usable_password_is_told_the_remedy():
    actor = {"id": 4242, "username": "no-pw-actor",
             "password_hash": unusable_password_hash(), "locked_until": None}
    with pytest.raises(HTTPException) as exc:
        require_step_up(actor, "whatever", "create an account")
    assert exc.value.status_code == 400 and "Owner" in exc.value.detail
