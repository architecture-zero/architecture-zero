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


def test_the_help_collection_is_never_a_department(monkeypatch):
    """In-product help (2026-09-21): the product's own pages live in kb_help,
    which uses the department machinery and is NOT a department - never
    listed, never residue, never counted. One predicate,
    _is_corpus_collection, decides it for every enumeration."""
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_help", 30),
        _Col("kb_restricted", 3),
        _Col("knowledge_base", 10),
    ]))
    assert database.list_departments() == ["general", "restricted"]
    assert database.department_residue() == []
    assert database.count_documents() == 13
    assert database._collection_name(database.HELP_DEPARTMENT) == database.HELP_COLLECTION


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


# -- A department spelled like "general" IS general (2026-10-26) --------------
#
# Until then only the exact spelling reached the global collection: "General"
# minted kb_general, every listing labeled it by its suffix - "general", the
# global collection's label - and a delete aimed at it from a listing hit the
# global collection. The sanitized name decides now; a kb_general minted before
# the fix is kept out of every listing and named at boot, never deleted.

class _NamedCol(_Col):
    """A collection holding one chunk per named source, for listing and delete."""
    def __init__(self, name, sources):
        super().__init__(name, len(sources))
        self._sources = list(sources)
        self.deleted = []

    def get(self, where=None, include=None):
        rows = [(f"{self.name}-{i}", s) for i, s in enumerate(self._sources)
                if where is None or where.get("source") == s]
        return {"ids": [i for i, _ in rows],
                "metadatas": [{"source": s} for _, s in rows]}

    def delete(self, ids=None):
        self.deleted.extend(ids)


def test_every_spelling_of_general_is_the_global_collection():
    for spelling in ("general", "General", "GENERAL", "gEnErAl", "", None):
        assert database._collection_name(spelling) == database.GLOBAL_COLLECTION, spelling
    # A name that only CONTAINS it is a department of its own, and its label
    # cannot be mistaken for "general".
    assert database._collection_name("General Ledger") == "kb_general_ledger"
    assert database._collection_name(" general") == "kb__general"


def test_a_general_spelling_mints_no_collection_of_its_own(monkeypatch):
    client = _MintTrackingClient([_Col("knowledge_base", 4)])
    monkeypatch.setattr(database, "client", client)
    database._get_collection("General")
    assert client.minted == []


def test_a_legacy_kb_general_never_wears_the_general_label(monkeypatch):
    """The collision itself: the legacy collection's sources used to merge
    into the global collection's under one "general" label (a shared name
    summed into one count), and the department list said "general" twice."""
    monkeypatch.setattr(database, "client", _MintTrackingClient([
        _NamedCol("knowledge_base", ["handbook.md"]),
        _NamedCol("kb_general", ["handbook.md", "ledger.md"]),
        _NamedCol("kb_restricted", ["ops.md"]),
    ]))
    # The per-source count's key differs by surface ("count" / "chunks").
    assert [(r["source"], r["department"], r.get("count", r.get("chunks")))
            for r in database.list_sources()] == [
        ("handbook.md", "general", 1),
        ("ops.md", "restricted", 1),
    ]
    assert database.list_departments() == ["general", "restricted"]
    assert database.department_residue() == []
    assert database.count_documents() == 2


def test_a_delete_aimed_at_general_never_reaches_the_legacy_collection(monkeypatch):
    """Every spelling deletes from the global collection; the legacy one is
    the operator's to move (report, don't destroy)."""
    glob = _NamedCol("knowledge_base", ["handbook.md"])
    legacy = _NamedCol("kb_general", ["handbook.md"])
    monkeypatch.setattr(database, "client", _MintTrackingClient([glob, legacy]))
    monkeypatch.setattr(database, "_invalidate_lexical_index",
                        lambda *a, **k: None, raising=False)
    for spelling in ("general", "General"):
        database.delete_source("handbook.md", spelling)
    assert legacy.deleted == []
    assert glob.deleted == ["knowledge_base-0", "knowledge_base-0"]


def test_a_legacy_kb_general_is_named_not_hidden(monkeypatch):
    monkeypatch.setattr(database, "client", _Client([
        _Col("knowledge_base", 3), _Col("kb_general", 7)]))
    assert database.legacy_general_count() == 7
    monkeypatch.setattr(database, "client", _Client([_Col("knowledge_base", 3)]))
    assert database.legacy_general_count() is None


def test_the_endpoint_lists_general_once(client, admin_headers, monkeypatch):
    monkeypatch.setattr(database, "client", _Client([
        _Col("kb_general", 5), _Col("kb_restricted", 2)]))
    r = client.get("/api/ingest/departments", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["departments"] == ["general", "restricted"]
