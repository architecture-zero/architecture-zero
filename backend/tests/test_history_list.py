"""The chat sidebar's History list (2026-10-03, the 2026-08-01 UI audit's
sessions drawer): conversations grouped by their last activity, renamed in
place, and deleted whole.

Pinned here: the list carries each conversation's last activity; a rename is
owner-scoped, validated, and works on every conversation the list offers -
including one with no meta row yet; deleting a conversation deletes its name,
which the chat route derives from its first prompt, with its messages; and the
guest purge ages the names out with the messages they belong to.
"""
from datetime import datetime, timedelta

import app.history as h
from app.db import get_session
from app.models import Message


def _msg(session: str, user_id: int | None, ts: str, role: str = "user", content: str = "x"):
    with get_session() as db:
        db.add(Message(session=session, user_id=user_id, role=role, content=content, timestamp=ts))


def _member(client, admin_headers, username: str):
    password = "MemberPass1!"
    client.post("/api/users",
                json={"current_password": "AdminPass1", "username": username, "password": password,
                      "role": "member", "department": "general"},
                headers=admin_headers)
    tok = client.post("/api/auth/login",
                      json={"username": username, "password": password}).json()["access_token"]
    headers = {"Authorization": f"Bearer {tok}"}
    return headers, client.get("/api/auth/me", headers=headers).json()["id"]


def _mine(client, headers) -> dict:
    r = client.get("/api/sessions/mine", headers=headers)
    assert r.status_code == 200, r.text
    return {s["session"]: s for s in r.json()["sessions"]}


def test_the_list_carries_each_conversations_last_activity(client, admin_headers):
    me = client.get("/api/auth/me", headers=admin_headers).json()["id"]
    _msg("hist-last-at", me, "2026-01-01T09:00:00")
    _msg("hist-last-at", me, "2026-01-05T17:30:00", role="assistant")
    row = _mine(client, admin_headers)["hist-last-at"]
    assert row["started"] == "2026-01-01T09:00:00"
    assert row["last_at"] == "2026-01-05T17:30:00"


def test_a_rename_is_owner_scoped_validated_and_reaches_every_listed_conversation(client, admin_headers):
    me = client.get("/api/auth/me", headers=admin_headers).json()["id"]
    # Messages and NO meta row: the chat route names a session only on a first
    # turn sent without history, and the rename used to 404 on such a row
    # although the list offered it.
    _msg("hist-rename", me, "2026-02-01T00:00:00")
    assert h.get_session_meta("hist-rename", me) is None
    r = client.patch("/api/sessions/hist-rename", json={"name": "  Q3 planning  "}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Q3 planning"
    assert _mine(client, admin_headers)["hist-rename"]["name"] == "Q3 planning"

    # Blank and over-long names are refused, and the stored name stands.
    assert client.patch("/api/sessions/hist-rename", json={"name": "   "},
                        headers=admin_headers).status_code == 400
    assert client.patch("/api/sessions/hist-rename", json={"name": "x" * 301},
                        headers=admin_headers).status_code == 400
    assert h.get_session_meta("hist-rename", me)["name"] == "Q3 planning"

    # Another account finds nothing to rename - and gets no row of its own out
    # of trying.
    other_headers, other_id = _member(client, admin_headers, "hist-renamer")
    assert client.patch("/api/sessions/hist-rename", json={"name": "hijack"},
                        headers=other_headers).status_code == 404
    assert h.get_session_meta("hist-rename", other_id) is None
    assert h.get_session_meta("hist-rename", me)["name"] == "Q3 planning"


def test_deleting_a_conversation_deletes_its_name(client, admin_headers):
    me = client.get("/api/auth/me", headers=admin_headers).json()["id"]
    _msg("hist-delete", me, "2026-03-01T00:00:00")
    h.upsert_session_meta("hist-delete", name="my private first prompt", user_id=me)
    assert client.delete("/api/history/hist-delete", headers=admin_headers).status_code == 200
    assert h.get_session_meta("hist-delete", me) is None
    assert h.load_history("hist-delete", me) == []

    # Owner-scoped: another account's delete of the same id touches nothing.
    _msg("hist-delete-kept", me, "2026-03-02T00:00:00")
    h.upsert_session_meta("hist-delete-kept", name="still mine", user_id=me)
    other_headers, _ = _member(client, admin_headers, "hist-deleter")
    assert client.delete("/api/history/hist-delete-kept", headers=other_headers).status_code == 200
    assert h.get_session_meta("hist-delete-kept", me)["name"] == "still mine"
    assert len(h.load_history("hist-delete-kept", me)) == 1


def test_the_guest_purge_ages_out_the_names_with_the_messages(client):
    old = (datetime.utcnow() - timedelta(days=40)).isoformat()
    fresh = datetime.utcnow().isoformat()
    _msg("hist-guest-old", None, old)
    h.upsert_session_meta("hist-guest-old", name="a guest's first prompt", user_id=None)
    _msg("hist-guest-fresh", None, fresh)
    h.upsert_session_meta("hist-guest-fresh", name="a recent guest's prompt", user_id=None)

    out = h.purge_anonymous_sessions(30)
    assert out["names"] >= 1
    assert h.get_session_meta("hist-guest-old", None) is None
    assert h.load_history("hist-guest-old", None) == []
    assert h.get_session_meta("hist-guest-fresh", None)["name"] == "a recent guest's prompt"
