"""The chat and view_history scopes, asked for by the routes that use them
(outside review 2026-10-06, AZ-03; fixed 2026-10-07).

THE GAP. The taxonomy defines "chat" (use the chat interface) and
"view_history" (see own conversation history), and an operator can take
either away with an explicit permission list - but only /api/sessions/mine
asked. The chat route, reading or deleting a conversation, the regenerate
step and the session create / rename / delete routes asked for a login and
nothing more, so removing a scope removed the sidebar and left the
capability. History stays owner-scoped underneath; this is about the scope.

Each test fails on the code before the fix.
"""
import uuid

import pytest

from app.db import get_session
from app.models import User
from app.users import update_user_permissions

_REFUSED_CHAT = "Permission required: chat"
_REFUSED_HISTORY = "Permission required: view_history"


def _account(client, admin_headers, perms):
    """A member, then an explicit permission list - what an operator's
    per-user override stores - and a session for it."""
    name, pw = f"scope-{uuid.uuid4().hex[:8]}", "ScopePass1"
    for role in ("member", "user"):
        r = client.post("/api/users", headers=admin_headers,
                        json={"current_password": "AdminPass1", "username": name,
                              "password": pw, "role": role})
        if r.status_code in (200, 201):
            break
    with get_session() as db:
        uid = db.query(User).filter(User.username == name).one().id
    if perms is not None:
        update_user_permissions(uid, perms)
    r = client.post("/api/auth/login", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, uid


def _detail(r):
    try:
        return r.json().get("detail")
    except Exception:
        return None


def test_an_account_without_chat_cannot_chat_start_or_regenerate(client, admin_headers):
    h, _ = _account(client, admin_headers, ["view_history"])
    r = client.post("/api/chat", headers=h, json={"prompt": "hello", "session_id": "s-chat"})
    assert r.status_code == 403 and _detail(r) == _REFUSED_CHAT, (r.status_code, r.text[:200])
    r = client.post("/api/sessions", headers=h, json={"session_id": "s-chat"})
    assert r.status_code == 403 and _detail(r) == _REFUSED_CHAT
    r = client.delete("/api/history/s-chat/tail", headers=h)
    assert r.status_code == 403 and _detail(r) == _REFUSED_CHAT
    # What it does hold still works.
    assert client.get("/api/history/s-chat", headers=h).status_code == 200


def test_an_account_without_view_history_cannot_read_rename_or_remove_it(
        client, admin_headers):
    h, _ = _account(client, admin_headers, ["chat"])
    for method, path, kw in (("get", "/api/history/s-hist", {}),
                             ("delete", "/api/history/s-hist", {}),
                             ("patch", "/api/sessions/s-hist", {"json": {"name": "x"}}),
                             ("delete", "/api/sessions/s-hist", {}),
                             ("get", "/api/sessions/mine", {})):
        r = getattr(client, method)(path, headers=h, **kw)
        assert r.status_code == 403 and _detail(r) == _REFUSED_HISTORY, (method, path, r.text)
    assert client.post("/api/sessions", headers=h,
                       json={"session_id": "s-hist"}).status_code == 200


def test_the_reviews_case_an_account_holding_only_manage_kb(client, admin_headers):
    """The review's own reproduction: ['manage_kb'] read its own history."""
    h, _ = _account(client, admin_headers, ["manage_kb"])
    assert client.get("/api/history/anything", headers=h).status_code == 403
    r = client.post("/api/chat", headers=h, json={"prompt": "hello", "session_id": "s-kb"})
    assert r.status_code == 403 and _detail(r) == _REFUSED_CHAT


def test_a_role_preset_and_a_role_change_restore_both(client, admin_headers):
    """A member's preset holds both scopes; a role change resets an explicit
    list to the preset (users.update_user_role), so both come back."""
    h, uid = _account(client, admin_headers, None)
    assert client.get("/api/history/s-member", headers=h).status_code == 200
    r = client.post("/api/chat", headers=h, json={"prompt": "hello", "session_id": "s-member"})
    assert _detail(r) != _REFUSED_CHAT

    h2, uid2 = _account(client, admin_headers, ["manage_kb"])
    from app.users import update_user_role
    update_user_role(uid2, "member")
    assert client.get("/api/history/s-member2", headers=h2).status_code == 200
