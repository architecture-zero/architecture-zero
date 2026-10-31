"""The KB's listing and review surfaces read what the chat reads (2026-10-31).

The chat reads, for an account below the Owner, the shared department plus
the account's OWN department, each only where the account's level clears the
department's floor (rerank.retrieve). The KB tab's manage_kb surfaces - the
source listing (with or without ?department=), the PII and injection
listings, and the quarantine queue, whose rows carry up to a thousand
characters of the held text - checked the level alone. On a deployment that
shares two departments at the Member floor, an Admin in one of them read the
other's source names and held text there, while the chat never gives that
account a word of it.

FIX: below the Owner, these surfaces show the shared department and the
caller's own, compared as the collections they key (database._collection_name,
the write check's comparison), and still only where the level clears the
floor. CONTROL: the Owner reads every department; the shared department is
everyone's.
"""
import pytest

from app.db import get_session

_SALES_ADMIN = {"username": "ownaxis-admin", "password": "AdminRole1"}

# What the index lists on a deployment with two shared departments. The
# labels are the collection names, as database.list_sources writes them.
_LISTED = [
    {"source": "faq.md", "count": 2, "department": "general"},
    {"source": "hr-handbook.md", "count": 5, "department": "hr"},
    {"source": "pipeline.md", "count": 3, "department": "sales"},
]


@pytest.fixture
def shared_departments(monkeypatch):
    """The operator shares hr and sales at the Member floor - the documented
    way a department is opened below the Owner (rag_config)."""
    from app import rag_config
    monkeypatch.setitem(rag_config.DEPARTMENT_MIN_LEVEL, "hr", 1)
    monkeypatch.setitem(rag_config.DEPARTMENT_MIN_LEVEL, "sales", 1)


@pytest.fixture
def sales_admin(client, admin_headers):
    """An Admin-role account whose department is stored as typed, "Sales" -
    the same collection as the listing's label "sales"."""
    client.post("/api/users", headers=admin_headers,
                json={"current_password": "AdminPass1", **_SALES_ADMIN,
                      "role": "admin", "department": "Sales"})   # idempotent across tests
    r = client.post("/api/auth/login", json=_SALES_ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _stub_listings(monkeypatch):
    monkeypatch.setattr("app.routers.kb.list_sources",
                        lambda department=None: [s for s in _LISTED
                                                 if department is None or s["department"] == department])
    monkeypatch.setattr("app.routers.kb.list_pii_sources", lambda: list(_LISTED))
    monkeypatch.setattr("app.database.list_injection_flagged_sources", lambda: list(_LISTED))


def test_the_source_listings_show_the_shared_and_the_callers_own_department(
        client, admin_headers, sales_admin, shared_departments, monkeypatch):
    _stub_listings(monkeypatch)
    for path in ("/api/ingest/sources", "/api/admin/pii-sources", "/api/admin/injection-sources"):
        r = client.get(path, headers=sales_admin)
        assert r.status_code == 200, f"{path}: {r.text}"
        assert [s["source"] for s in r.json()["sources"]] == ["faq.md", "pipeline.md"], path
        assert "hr-handbook" not in r.text, path
        # CONTROL: the Owner reads every department.
        r = client.get(path, headers=admin_headers)
        assert [s["source"] for s in r.json()["sources"]] == [s["source"] for s in _LISTED], path


def test_asking_for_another_department_by_name_lists_nothing(
        client, admin_headers, sales_admin, shared_departments, monkeypatch):
    _stub_listings(monkeypatch)
    r = client.get("/api/ingest/sources", params={"department": "hr"}, headers=sales_admin)
    assert r.status_code == 200 and r.json()["sources"] == []
    r = client.get("/api/ingest/sources", params={"department": "sales"}, headers=sales_admin)
    assert [s["source"] for s in r.json()["sources"]] == ["pipeline.md"]
    r = client.get("/api/ingest/sources", params={"department": "hr"}, headers=admin_headers)
    assert [s["source"] for s in r.json()["sources"]] == ["hr-handbook.md"]


def test_the_quarantine_queue_shows_the_shared_and_the_callers_own_department(
        client, admin_headers, sales_admin, shared_departments):
    from app.models import QuarantinedDoc
    with get_session() as db:
        for dept in ("general", "hr", "sales"):
            db.add(QuarantinedDoc(source=f"ownaxis-{dept}.txt", department=dept,
                                  trust_tier="untrusted", findings="[]",
                                  text=f"HELD {dept.upper()} TEXT", status="held",
                                  created_at="2026-10-31T00:00:00"))
    r = client.get("/api/admin/kb/quarantine", headers=sales_admin)
    assert r.status_code == 200, r.text
    sources = {i["source"] for i in r.json()["items"] if i["source"].startswith("ownaxis-")}
    assert sources == {"ownaxis-general.txt", "ownaxis-sales.txt"}
    assert "HELD HR TEXT" not in r.text
    # CONTROL: the Owner reads every row.
    r = client.get("/api/admin/kb/quarantine", headers=admin_headers)
    assert {"ownaxis-general.txt", "ownaxis-hr.txt", "ownaxis-sales.txt"} <= {
        i["source"] for i in r.json()["items"]}
