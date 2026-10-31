"""The ingestion queue and the sync's report read what the chat reads
(2026-10-31).

The KB tab's source listings and the quarantine queue show an account below
the Owner the shared department and its own, each where its level clears the
floor (routers.kb._clears_department). Two surfaces of the same shape were
left on the scope alone: GET /api/admin/jobs listed every department's job
rows - source names and departments - to any manage_kb holder, and POST
/api/kb/sync answered every synced source by name, Owner-only internal files
included, to the same caller the file listing hides them from.

FIX: below the Owner, the queue lists only the rows the caller clears - the
newest `limit` of THOSE, so another department's newer batch does not empty
it - and the sync's per-source report leaves out the names it does not clear.
CONTROL: the Owner sees every row and every name.
"""
import pytest

from app import jobs
from app.db import get_session

_SALES_ADMIN = {"username": "jobaxis-admin", "password": "AdminRole1"}


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


@pytest.fixture
def job_rows():
    """Oldest first: the caller's two, then two newer it does not clear."""
    made = [jobs.create_job(f"jobaxis-{d}.md", d)
            for d in ("general", "sales", "hr", "restricted")]
    yield made
    from app.models import IngestJob
    with get_session() as s:
        s.query(IngestJob).filter(IngestJob.job_id.in_(made)).delete(synchronize_session=False)


def _sources(r) -> list[str]:
    return [j["source"] for j in r.json()["jobs"] if j["source"].startswith("jobaxis-")]


def test_the_queue_lists_the_shared_and_the_callers_own_department(
        client, admin_headers, sales_admin, shared_departments, job_rows):
    r = client.get("/api/admin/jobs", params={"limit": 200}, headers=sales_admin)
    assert r.status_code == 200, r.text
    assert sorted(_sources(r)) == ["jobaxis-general.md", "jobaxis-sales.md"]
    assert "jobaxis-hr" not in r.text and "jobaxis-restricted" not in r.text
    # CONTROL: the Owner sees every row.
    r = client.get("/api/admin/jobs", params={"limit": 200}, headers=admin_headers)
    assert sorted(_sources(r)) == [f"jobaxis-{d}.md" for d in ("general", "hr", "restricted", "sales")]


def test_the_limit_counts_the_rows_the_caller_clears(
        client, sales_admin, shared_departments, job_rows):
    """The two newest rows are another department's; the caller's two still
    fill a limit of two."""
    r = client.get("/api/admin/jobs", params={"limit": 2}, headers=sales_admin)
    assert r.status_code == 200, r.text
    assert _sources(r) == ["jobaxis-sales.md", "jobaxis-general.md"]


def test_the_sync_report_names_only_what_the_caller_clears(
        client, admin_headers, sales_admin, monkeypatch):
    monkeypatch.setattr("app.routers.kb._sync_knowledge_dir",
                        lambda: {"faq.md": {"status": "ok", "chunks": 1},
                                 "internal/plan.md": {"status": "ok", "chunks": 3}})
    monkeypatch.setattr("app.routers.kb._sync_docs",
                        lambda: {"docs/guide.md": {"status": "skipped"}})
    monkeypatch.setattr("app.system_records.sync_system_records", lambda: {})
    monkeypatch.setattr("app.runtime_config.HELP_DOCS_SYNC", False)
    r = client.post("/api/kb/sync", headers=sales_admin)
    assert r.status_code == 200, r.text
    assert list(r.json()["files"]) == ["faq.md"]
    assert list(r.json()["docs"]) == ["docs/guide.md"]
    assert "internal/plan.md" not in r.text
    # CONTROL: the Owner's report names every source.
    r = client.post("/api/kb/sync", headers=admin_headers)
    assert sorted(r.json()["files"]) == ["faq.md", "internal/plan.md"]
