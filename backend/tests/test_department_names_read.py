"""The department NAMES a user manager assigns (2026-10-30).

The Users tab's department picker reads GET /api/ingest/departments, which
took manage_kb alone - so an account holding manage_users without manage_kb
got a "Loading departments failed" toast and a picker of `general` alone.
That account already creates a user in any department (POST /api/users),
moves one between departments (PATCH /api/users/{id}/department) and reads
every account's department (GET /api/users); the list of names is below the
writes it holds. The route now admits manage_users OR manage_kb; it serves
names only, and what a department holds stays behind the read axis.

FIX: a manage_users-only account reads the list. CONTROLS: a manage_kb-only
account still reads it, and an account holding neither (but other scopes) is
refused.
"""
import uuid

import pytest

from app.routers import kb

NAMES = ["general", "sales"]


@pytest.fixture
def scoped(client, monkeypatch):
    """Headers for a fresh member account holding exactly the scopes given;
    each account is deactivated at teardown (the suite shares one database).
    The store's department list is stubbed: these tests are about who may
    read it, not what the store holds."""
    from app.jwt_auth import create_access_token, hash_password
    from app.users import create_user, deactivate_user, update_user_permissions
    monkeypatch.setattr(kb, "list_departments", lambda: list(NAMES))
    made = []

    def make(permissions):
        name = f"deptnames-{uuid.uuid4().hex[:8]}"
        uid = create_user(name, hash_password("DeptNames1"), role="member")
        made.append(uid)
        update_user_permissions(uid, permissions)
        return {"Authorization": "Bearer " + create_access_token(uid, name, "member")}

    yield make
    for uid in made:
        deactivate_user(uid)


def test_a_user_manager_reads_the_department_names(client, scoped):
    """FIX: the Users tab's picker loads for a manage_users-only account."""
    r = client.get("/api/ingest/departments", headers=scoped(["chat", "manage_users"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"departments": NAMES}


def test_a_knowledge_base_manager_still_reads_them(client, scoped):
    """CONTROL: the KB tab's reader is unchanged."""
    r = client.get("/api/ingest/departments", headers=scoped(["chat", "manage_kb"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"departments": NAMES}


def test_an_account_with_neither_scope_is_refused(client, scoped):
    """CONTROL: holding other operator scopes is not enough."""
    r = client.get("/api/ingest/departments",
                   headers=scoped(["chat", "view_history", "view_analytics", "view_audit_log"]))
    assert r.status_code == 403, r.text
    assert "sales" not in r.text
