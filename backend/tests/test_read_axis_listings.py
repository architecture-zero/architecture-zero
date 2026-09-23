"""The read axis on the KB's listing and review surfaces (2026-09-23).

permissions.py separates two axes: a permission SCOPE gates actions, the
clearance LEVEL gates reads ("an admin who manages users still must not read
owner-only content"). These tests hold the KB's manage_kb surfaces to the
second axis: the four source listings and the quarantine queue show an Admin
(level 2, the preset holds manage_kb) only the departments its level clears,
the Owner (level 3) everything, and discard on a row above the caller's level
answers as absent. The /metrics session fallback holds the view_analytics
scope. The route pin in test_route_authz_wiring.py carries the declared
guards; this file carries the behaviour those pins name.
"""
import pytest

from app.db import get_session

_ADMIN_ROLE = {"username": "readaxis-admin", "password": "AdminRole1"}
_MEMBER = {"username": "readaxis-member", "password": "MemberPass1"}


def _headers_for(client, owner_headers, creds, role):
    client.post("/api/users", headers=owner_headers,
                json={"current_password": "AdminPass1", **creds, "role": role})  # idempotent: dup -> 4xx, login still works
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, f"{role} login failed: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin_role_headers(client, admin_headers):
    """An ADMIN-role token. conftest's admin_headers is the setup account,
    which /api/auth/setup makes the OWNER - so that fixture is the Owner in
    every test below, and this one is the rung under it."""
    return _headers_for(client, admin_headers, _ADMIN_ROLE, "admin")


@pytest.fixture
def member_headers(client, admin_headers):
    return _headers_for(client, admin_headers, _MEMBER, "member")


_LISTED = [
    {"source": "faq.md", "count": 2, "department": "general"},
    {"source": "internal/plan.md", "count": 9, "department": "restricted"},
    {"source": "internal/session-log.md", "count": 40, "department": "history"},
    {"source": "new-collection.md", "count": 1, "department": "brand-new"},   # unlisted -> Owner-only
]


def test_quarantine_queue_shows_only_the_departments_the_caller_clears(
        client, admin_headers, admin_role_headers):
    from app.models import QuarantinedDoc
    with get_session() as db:
        db.add(QuarantinedDoc(source="readaxis-general.txt", department="general",
                              trust_tier="untrusted", findings="[]",
                              text="HELD GENERAL TEXT", status="held",
                              created_at="2026-09-23T00:00:00"))
        db.add(QuarantinedDoc(source="readaxis-restricted.txt", department="restricted",
                              trust_tier="untrusted", findings="[]",
                              text="HELD RESTRICTED TEXT", status="held",
                              created_at="2026-09-23T00:00:00"))
    with get_session() as db:
        ids = {r.source: r.id for r in db.query(QuarantinedDoc)
               .filter(QuarantinedDoc.source.like("readaxis-%")).all()}

    r = client.get("/api/admin/kb/quarantine", headers=admin_role_headers)
    assert r.status_code == 200, r.text
    sources = {i["source"] for i in r.json()["items"]}
    assert "readaxis-general.txt" in sources
    assert "readaxis-restricted.txt" not in sources
    assert "HELD RESTRICTED TEXT" not in r.text

    r = client.get("/api/admin/kb/quarantine", headers=admin_headers)
    assert r.status_code == 200
    assert {"readaxis-general.txt", "readaxis-restricted.txt"} <= {i["source"] for i in r.json()["items"]}

    # Discard follows the listing: a row above the caller's level answers as
    # absent; a row the caller clears is theirs to discard (the Admin keeps
    # its review role); the Owner reaches every row.
    assert client.delete(f"/api/admin/kb/quarantine/{ids['readaxis-restricted.txt']}",
                         headers=admin_role_headers).status_code == 404
    assert client.delete(f"/api/admin/kb/quarantine/{ids['readaxis-general.txt']}",
                         headers=admin_role_headers).status_code == 200
    assert client.delete(f"/api/admin/kb/quarantine/{ids['readaxis-restricted.txt']}",
                         headers=admin_headers).status_code == 200


def test_kb_files_omits_restricted_names_below_the_floor(client, admin_headers, admin_role_headers,
                                                         tmp_path, monkeypatch):
    """The classifier decides: a name in RESTRICTED_SOURCES is Owner-only,
    faq.md is the general floor. An Admin sees one name; the Owner sees both."""
    (tmp_path / "faq.md").write_text("public floor", encoding="utf-8")
    (tmp_path / "board-minutes.md").write_text("owner only", encoding="utf-8")
    monkeypatch.setattr("app.routers.kb.KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr("app.rag_config.RESTRICTED_SOURCES", {"board-minutes.md"})

    r = client.get("/api/kb/files", headers=admin_role_headers)
    assert r.status_code == 200, r.text
    assert [f["name"] for f in r.json()["files"]] == ["faq.md"]
    assert "board-minutes" not in r.text

    r = client.get("/api/kb/files", headers=admin_headers)
    assert r.status_code == 200
    assert [f["name"] for f in r.json()["files"]] == ["board-minutes.md", "faq.md"]


def test_ingest_sources_omits_departments_below_the_floor(client, admin_headers, admin_role_headers,
                                                          monkeypatch):
    # The stub honours the department argument the way database.list_sources
    # does (an explicit department returns only that collection's rows).
    monkeypatch.setattr("app.routers.kb.list_sources",
                        lambda department=None: [s for s in _LISTED
                                                 if department is None or s["department"] == department])
    r = client.get("/api/ingest/sources", headers=admin_role_headers)
    assert r.status_code == 200, r.text
    assert [s["source"] for s in r.json()["sources"]] == ["faq.md"]
    r = client.get("/api/ingest/sources", params={"department": "restricted"}, headers=admin_role_headers)
    assert r.status_code == 200 and r.json()["sources"] == []
    r = client.get("/api/ingest/sources", headers=admin_headers)
    assert [s["source"] for s in r.json()["sources"]] == [s["source"] for s in _LISTED]


def test_pii_and_injection_sources_omit_departments_below_the_floor(client, admin_headers,
                                                                    admin_role_headers, monkeypatch):
    monkeypatch.setattr("app.routers.kb.list_pii_sources", lambda: list(_LISTED))
    monkeypatch.setattr("app.database.list_injection_flagged_sources", lambda: list(_LISTED))
    for path in ("/api/admin/pii-sources", "/api/admin/injection-sources"):
        r = client.get(path, headers=admin_role_headers)
        assert r.status_code == 200, f"{path}: {r.text}"
        assert [s["source"] for s in r.json()["sources"]] == ["faq.md"], path
        assert "mode" in r.json()
        r = client.get(path, headers=admin_headers)
        assert [s["source"] for s in r.json()["sources"]] == [s["source"] for s in _LISTED], path


def test_metrics_session_fallback_needs_view_analytics(client, admin_headers, admin_role_headers,
                                                       member_headers, monkeypatch):
    """With no scrape token set, the session half of _metrics_auth holds the
    view_analytics scope: a Member is refused, the Admin preset and the Owner
    are served. test_hardening.py carries the token half."""
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    r = client.get("/metrics", headers=member_headers)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Permission required: view_analytics"
    assert client.get("/metrics", headers=admin_role_headers).status_code == 200
    r = client.get("/metrics", headers=admin_headers)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
