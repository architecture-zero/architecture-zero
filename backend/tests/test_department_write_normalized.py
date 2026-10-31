"""A department write compares collections, not spellings (2026-10-31).

_check_department_write lets a non-Owner write (ingest, upload, delete) only
into general or their own department. It compared the names as typed, while
the index keys a department by its normalized collection name
(database._collection_name: lowercased, every other character an underscore).
So an account stored in "Finance Team" was refused a delete from its own
department as the listing names it - "finance_team", the collection's label -
and refused a write to "General", which is the global collection. Both sides
of the comparison name one collection, so the collection is what is compared.

FIX: the stored spelling and the listing's spelling of the caller's own
department are allowed at all three doors, and so is any spelling of general.
CONTROL: another department is refused (a near spelling that keys a different
collection included), the Owner writes anywhere, and the reserved help name
stays refused for everyone.
"""
import pytest
from fastapi import HTTPException

from app.routers import kb

_FINANCE_ADMIN = {"username": "finance-team-admin", "password": "AdminRole1"}


def _user(department, role="admin"):
    return {"id": 99, "username": "u", "role": role, "department": department}


@pytest.mark.parametrize("target", ["Finance Team", "finance_team", "FINANCE TEAM",
                                    "general", "General", "GENERAL"])
def test_own_department_and_general_allowed_in_any_spelling(target):
    kb._check_department_write(_user("Finance Team"), target)


# Each of these keys a collection of its own: " general" is kb__general, not
# the global collection (test_department_invariant pins that derivation).
@pytest.mark.parametrize("target", ["restricted", "finance", "finance-team",
                                    "Finance Team Archive", " general"])
def test_another_department_is_refused(target):
    with pytest.raises(HTTPException) as err:
        kb._check_department_write(_user("Finance Team"), target)
    assert err.value.status_code == 403
    assert err.value.detail == f"Not permitted to ingest into department: {target}"


def test_the_owner_writes_anywhere():
    kb._check_department_write(_user("support", role="owner"), "restricted")


def test_the_help_name_stays_reserved_for_everyone():
    for role in ("owner", "admin"):
        with pytest.raises(HTTPException) as err:
            kb._check_department_write(_user("help", role=role), "Help")
        assert err.value.status_code == 400


@pytest.fixture
def finance_headers(client, admin_headers):
    client.post("/api/users", headers=admin_headers,
                json={"current_password": "AdminPass1", **_FINANCE_ADMIN,
                      "role": "admin", "department": "Finance Team"})  # idempotent: dup -> 4xx
    r = client.post("/api/auth/login", json=_FINANCE_ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_the_three_doors_take_the_listing_spelling(client, finance_headers, monkeypatch):
    """The filed case, at the routes: the account is stored in "Finance Team"
    and the listing names its collection "finance_team"."""
    deleted, added = [], []
    monkeypatch.setattr(kb, "delete_source", lambda s, department=None: deleted.append(department))
    monkeypatch.setattr(kb, "chunk_metadata", lambda ids, dept: {})
    monkeypatch.setattr(kb, "add_document", lambda *a, **k: added.append(k.get("department")))
    monkeypatch.setattr(kb, "_ingest_upload",
                        lambda name, ext, data, dept, user: {"status": "ingested"})

    r = client.delete("/api/ingest/source/plan.md", params={"department": "finance_team"},
                      headers=finance_headers)
    assert r.status_code == 200, r.text
    assert deleted == ["finance_team"]

    r = client.post("/api/ingest", headers=finance_headers,
                    json={"doc_id": "fin-1", "text": "Quarter close notes.",
                          "metadata": {"source": "close.md"}, "department": "finance_team"})
    assert r.status_code == 200, r.text
    assert added == ["finance_team"]

    r = client.post("/api/ingest/upload", headers=finance_headers,
                    files={"file": ("close.md", b"Quarter close notes.", "text/markdown")},
                    data={"department": "finance_team"})
    assert r.status_code == 200, r.text

    # CONTROL: another department is still refused at the door, nothing written.
    r = client.delete("/api/ingest/source/plan.md", params={"department": "restricted"},
                      headers=finance_headers)
    assert r.status_code == 403, r.text
    assert deleted == ["finance_team"]
