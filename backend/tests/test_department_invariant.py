"""THE DEPARTMENT-LIST INVARIANT.

A department list holds only REAL departments - ones that actually hold
documents. The bug class this closes (found live on a derived instance): a
probe's cleanup honestly reported zero residual DOCUMENTS
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


# -- The root cause: a delete must not MINT the collection it deletes from ----

class _MintTrackingClient:
    """Records get_or_create calls so the test can prove none happened."""
    def __init__(self, cols):
        self._cols = list(cols)
        self.minted = []

    def list_collections(self):
        return list(self._cols)

    def get_collection(self, name):
        for c in self._cols:
            if c.name == name:
                return c
        raise ValueError(f"no such collection: {name}")

    def get_or_create_collection(self, name, metadata=None):
        # Faithful to chroma: get_or_create on an EXISTING collection creates
        # nothing and returns it. Recording every call as a "mint" would
        # conflate asking with creating - the exact confusion this whole fix
        # is about - and would fail the present-department case for a reason
        # that has nothing to do with the property under test.
        for c in self._cols:
            if c.name == name:
                return c
        self.minted.append(name)
        col = _Col(name, 0)
        self._cols.append(col)
        return col


def test_deleting_from_an_absent_department_mints_nothing(monkeypatch):
    """The root cause, pinned.

    A maintenance path that calls delete_source when it has nothing to write
    ("if not rows: delete_source('metrics-current', 'metrics')") used to reach
    through _get_collection, which is get_or_create - so a module with nothing
    to say minted an empty collection on every run, and list_departments()
    advertised it as a real department. Ordinary application code, not a
    probe: that is what a derived instance's boot report actually found.
    """
    client = _MintTrackingClient([_Col("kb_real", 5)])
    monkeypatch.setattr(database, "client", client)
    database.delete_source("metrics-current", "metrics")
    assert client.minted == [], f"delete minted collections: {client.minted}"
    assert database.list_departments() == ["general", "real"]


def test_deleting_from_a_present_department_still_deletes(monkeypatch):
    """The fix must not turn a real delete into a no-op."""
    deleted = {}

    class _DelCol(_Col):
        def get(self, where=None, include=None):
            return {"ids": ["a1"], "metadatas": [{"source": "s"}]}
        def delete(self, ids=None):
            deleted["ids"] = ids

    client = _MintTrackingClient([_DelCol("kb_present", 3)])
    monkeypatch.setattr(database, "client", client)
    # raising=False: this core carries the BM25 lexical index, but a derived
    # instance may not. This test is otherwise identical across derivations,
    # and a setattr that assumes the core's shape ERRORS on a derivation
    # rather than failing honestly - a ported test must not require machinery
    # the port lacks.
    monkeypatch.setattr(database, "_invalidate_lexical_index",
                        lambda *a, **k: None, raising=False)
    database.delete_source("s", "present")
    assert deleted.get("ids") == ["a1"]
    assert client.minted == []
