"""THE DEPARTMENT-LIST INVARIANT.

A department list holds only REAL departments - ones that actually hold
documents. The bug class this closes (found live on a derived instance,
2026-08-25): a probe's cleanup honestly reported zero residual DOCUMENTS
(delete_source removes documents) while its empty COLLECTIONS survived, and
list_departments() - which enumerates collections - served them as invented
departments on /api/ingest/departments. Worse, _get_collection's
get_or_create means even a delete or query naming a novel department mints an
empty collection as a side effect. Fixing the tool that caused it is not the
fix: this is the general form - residue cannot reach a department list AT
ALL, asserted at the database seam and on the endpoint, so any tool that
cleans up imperfectly discloses nothing.
"""
import logging

from app import database


class _Col:
    def __init__(self, name, count=0, broken=False):
        self.name = name
        self._count = count
        self._broken = broken

    def count(self):
        if self._broken:
            raise RuntimeError("index unreadable")
        return self._count


class _Client:
    def __init__(self, cols):
        self._cols = list(cols)

    def list_collections(self):
        return list(self._cols)


def test_empty_collections_never_reach_the_department_list(monkeypatch):
    """The residue class itself: an empty kb_* collection is not a department."""
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_restricted", 3),
        _Col("kb_stray_probe", 0),              # imperfect-cleanup leftover
        _Col("knowledge_base", 41),             # the global collection, not kb_*
    ]))
    assert database.list_departments() == ["general", "restricted"]
    assert database.department_residue() == ["stray_probe"]


def test_unreadable_count_fails_closed_to_residue(monkeypatch):
    """A collection whose count cannot be read is not PROVABLY real, so it must
    not ride a department list on faith - residue, loudly, until an operator
    looks."""
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_history", 7),
        _Col("kb_wounded", broken=True),
    ]))
    assert database.list_departments() == ["general", "history"]
    assert database.department_residue() == ["wounded"]


def test_general_survives_even_with_nothing_else(monkeypatch):
    """"general" is the global collection's label, real by construction - the
    invariant filters invented departments, it never empties the list."""
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_leftover_a", 0),
        _Col("kb_leftover_b", 0),
    ]))
    assert database.list_departments() == ["general"]
    assert database.department_residue() == ["leftover_a", "leftover_b"]


def test_exclusion_is_loud_not_silent(monkeypatch, caplog):
    """The invariant reports what it excluded. A guard that hides residue
    silently would just relocate the invisibility one layer down."""
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_probe_leftover", 0),
    ]))
    with caplog.at_level(logging.WARNING, logger="database"):
        database.list_departments()
    assert any("probe_leftover" in r.getMessage() for r in caplog.records)


def test_the_endpoint_never_serves_residue(client, admin_headers, monkeypatch):
    """The endpoint through the app: GET /api/ingest/departments must serve
    only real departments, whatever tool left the residue behind - including
    a typo'd delete that get_or_create quietly minted."""
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_restricted", 12),
        _Col("kb_restrcited", 0),               # the typo-mint shape
    ]))
    r = client.get("/api/ingest/departments", headers=admin_headers)
    assert r.status_code == 200
    depts = r.json()["departments"]
    assert "restricted" in depts
    assert "restrcited" not in depts
    assert "general" in depts


def test_the_endpoint_stays_authenticated(client):
    """The list is operator surface, not public - route-level auth holds even
    with ENABLE_AUTH=false, which is how conftest runs."""
    assert client.get("/api/ingest/departments").status_code == 401
