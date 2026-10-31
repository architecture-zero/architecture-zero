"""The quarantine's discard is a write door with the write check (2026-10-31).

A discard decides, for a department, that held content never enters it. The
upload, the ingest and the source delete each take _check_department_write
(the Owner anywhere; everyone else the shared department or its own); the
discard took manage_kb and the read axis alone, and the read axis was the
clearance level until that day - so an Admin in one department discarded
another's held upload on a deployment that shared both at the Member floor.
The release is Owner-only and needs nothing more: the Owner passes the write
check everywhere, and the release refuses the reserved help name itself.

FIX: a non-owner discards only in the shared department or its own (a row it
cannot read still answers as absent), through the same write check.
CONTROL: the Owner discards anywhere; the release stays the Owner's alone.
"""
import inspect

import pytest

from app.db import get_session

_SALES_ADMIN = {"username": "discard-admin", "password": "AdminRole1"}


@pytest.fixture
def shared_departments(monkeypatch):
    from app import rag_config
    monkeypatch.setitem(rag_config.DEPARTMENT_MIN_LEVEL, "hr", 1)
    monkeypatch.setitem(rag_config.DEPARTMENT_MIN_LEVEL, "sales", 1)


@pytest.fixture
def sales_admin(client, admin_headers):
    client.post("/api/users", headers=admin_headers,
                json={"current_password": "AdminPass1", **_SALES_ADMIN,
                      "role": "admin", "department": "Sales"})   # idempotent across tests
    r = client.post("/api/auth/login", json=_SALES_ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _held(dept: str) -> int:
    from app.models import QuarantinedDoc
    with get_session() as db:
        row = QuarantinedDoc(source=f"discard-{dept}.txt", department=dept,
                             trust_tier="untrusted", findings="[]", text="HELD",
                             status="held", created_at="2026-10-31T00:00:00")
        db.add(row)
        db.flush()
        return row.id


def _status(item_id: int) -> str:
    from app.models import QuarantinedDoc
    with get_session() as db:
        return db.get(QuarantinedDoc, item_id).status


def test_a_non_owner_discards_only_in_the_shared_department_or_its_own(
        client, admin_headers, sales_admin, shared_departments):
    hr, sales, general = _held("hr"), _held("sales"), _held("general")
    r = client.delete(f"/api/admin/kb/quarantine/{hr}", headers=sales_admin)
    assert r.status_code == 404, r.text
    assert _status(hr) == "held"
    assert client.delete(f"/api/admin/kb/quarantine/{sales}", headers=sales_admin).status_code == 200
    assert client.delete(f"/api/admin/kb/quarantine/{general}", headers=sales_admin).status_code == 200
    # CONTROL: the Owner discards in any department.
    assert client.delete(f"/api/admin/kb/quarantine/{hr}", headers=admin_headers).status_code == 200
    assert _status(hr) == "deleted"


def test_the_release_stays_the_owners_alone(client, sales_admin, shared_departments):
    sales = _held("sales")
    r = client.post(f"/api/admin/kb/quarantine/{sales}/release", headers=sales_admin)
    assert r.status_code == 403, r.text
    assert _status(sales) == "held"


def test_the_discard_asks_the_write_check():
    """The read axis decides what the caller sees; the write check decides
    what it may change, so a later change to either cannot open the other."""
    from app.routers import kb
    assert "_check_department_write(" in inspect.getsource(kb.admin_delete_quarantine)
