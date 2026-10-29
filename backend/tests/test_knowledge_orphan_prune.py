"""The knowledge orphan prune: a knowledge file deleted while the backend was
down is removed at the next sync.

The watcher removes a deleted knowledge file's chunks only while it is
running, and the startup sync pruned docs/ alone, so a file deleted between
processes kept serving forever.

What these pin: only sources the FILE SYNC wholly owns (every chunk stamped
from_file="true") can be pruned - an upload is named by its file name, the
same shape, and must survive, even when it replaces a candidate between the
listing and the prune's lock; a mixed source is reported, not deleted; the
help pages carry the same stamp and are never candidates; a missing or empty
knowledge dir prunes nothing; more candidates than the cap prunes nothing
unless forced; dry_run removes nothing; the sync and the admin endpoint
report what went; /api/ingest cannot claim the stamp. The CONTROL cases pass
on the code before the prune too: the docs/ prune and the legacy basename
purge are unchanged.
"""
import hashlib

import chromadb
import pytest
from chromadb.config import Settings as _RealSettings

import app.database as database
import app.ingest_sync as ingest_mod

EMBEDDING = [1.0, 0.0, 0.0, 0.0]


def _rows(*specs):
    """specs: (source, department, count, file_chunks)."""
    return [{"source": s, "department": d, "count": c, "file_chunks": f}
            for s, d, c, f in specs]


@pytest.fixture
def kdir(tmp_path, monkeypatch):
    k = tmp_path / "knowledge"
    k.mkdir()
    (k / "faq.md").write_text("# FAQ\n\nstill here.\n", encoding="utf-8")
    (k / "handbook").mkdir()
    (k / "handbook" / "leave.md").write_text("# Leave\n", encoding="utf-8")
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(k))
    monkeypatch.setattr(ingest_mod, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_mod, "_save_ingest_state", lambda s: None)
    return k


@pytest.fixture
def deletions(monkeypatch):
    gone = []
    monkeypatch.setattr(ingest_mod, "delete_source",
                        lambda source, dept=None: gone.append((source, dept)))
    return gone


class _FakeCol:
    """The slice of a chroma collection database.py uses here, with chroma's
    filter semantics for the two `where` shapes it sends (a field equality and
    an $and of them). Anything else raises rather than guessing."""

    def __init__(self, name):
        self.name = name
        self._rows: dict[str, tuple[str, dict]] = {}

    @staticmethod
    def _match(meta, where):
        if not where:
            return True
        if set(where) == {"$and"}:
            return all(_FakeCol._match(meta, w) for w in where["$and"])
        (key, want), = where.items()
        if isinstance(want, dict):
            raise NotImplementedError(f"operator filter {want!r}")
        return meta.get(key) == want

    def add(self, ids, documents, metadatas, embeddings=None):
        for i, d, m in zip(ids, documents, metadatas):
            self._rows[i] = (d, dict(m))

    upsert = add

    def update(self, ids, metadatas=None, documents=None, embeddings=None):
        # chroma 0.5's update as its API behaves, which the upload's re-stamp
        # is written against: the given metadata MERGES into the stored row (a
        # key it omits is kept), a None value is refused, and an id that is not
        # stored is skipped.
        for i, m in zip(ids, metadatas or [None] * len(ids)):
            if m is not None and any(v is None for v in m.values()):
                raise ValueError("Expected metadata value to be a str, int, float "
                                 "or bool, got None")
            if i in self._rows and m is not None:
                doc, held = self._rows[i]
                self._rows[i] = (doc, {**held, **m})

    def get(self, ids=None, where=None, include=None):
        hits = [(i, d, m) for i, (d, m) in self._rows.items()
                if (ids is None or i in ids) and self._match(m, where)]
        return {"ids": [h[0] for h in hits], "documents": [h[1] for h in hits],
                "metadatas": [h[2] for h in hits]}

    def delete(self, ids=None):
        for i in ids or []:
            self._rows.pop(i, None)

    def count(self):
        return len(self._rows)


class _FakeClient:
    def __init__(self):
        self._cols: dict[str, _FakeCol] = {}

    def list_collections(self):
        return list(self._cols.values())

    def create_collection(self, name, metadata=None):
        if name in self._cols:
            raise ValueError(f"collection {name} already exists")
        return self._cols.setdefault(name, _FakeCol(name))

    def get_or_create_collection(self, name, metadata=None):
        return self._cols.setdefault(name, _FakeCol(name))

    def get_collection(self, name):
        return self._cols[name]

    def delete_collection(self, name):
        del self._cols[name]


@pytest.fixture(params=["fake", "real"])
def index(request, monkeypatch):
    """The vector store behind database.py's seams, for the cases whose point
    is the metadata the store actually holds - run twice: once against real
    in-memory chroma, once against _FakeClient. The real run is the realism
    pin; the fake one runs on a host whose application-control policy refuses
    chroma's native index library, where the real run cannot start. Real
    wiring as in test_legacy_key_purge.py: conftest patches the chromadb
    namespace, so the real Settings comes from chromadb.config; the in-memory
    System is shared per settings, so start from (and leave) no collections
    of these names."""
    if request.param == "real":
        # A fresh System per test, as test_chroma_maintenance.py does: the
        # in-memory System is cached per settings, so no state carries over.
        from chromadb.api.shared_system_client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
        store = chromadb.EphemeralClient(
            settings=_RealSettings(is_persistent=False, anonymized_telemetry=False))
    else:
        store = _FakeClient()
    names = (database.GLOBAL_COLLECTION, database.HELP_COLLECTION, "kb_restricted")

    def drop_all():
        for name in names:
            try:
                store.delete_collection(name)
            except Exception:
                pass

    drop_all()
    monkeypatch.setattr(database, "client", store)
    # The production metadata, as every real collection is created with
    # (collection_metadata). list_sources creates the collection with it, so a
    # stand-in asking for other metadata on the same name made real chroma
    # refuse every later call - the end-to-end test's first sync errored.
    monkeypatch.setattr(database, "_get_collection",
                        lambda department=None: store.get_or_create_collection(
                            database._collection_name(department),
                            metadata=database.collection_metadata()))
    monkeypatch.setattr(database, "_embed_batch", lambda texts: [EMBEDDING] * len(texts))
    yield store
    drop_all()


def _sources(col) -> dict:
    out: dict = {}
    for m in col.get(include=["metadatas"])["metadatas"]:
        out[m["source"]] = out.get(m["source"], 0) + 1
    return out


# -- the prune itself -------------------------------------------------------------

def test_only_wholly_file_synced_sources_whose_file_is_gone_are_pruned(kdir, deletions, monkeypatch):
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: _rows(
        ("faq.md", "general", 3, 3),                    # on disk: kept
        ("handbook/leave.md", "general", 2, 2),         # on disk (subdir key): kept
        ("retired-guide.md", "general", 7, 7),          # file gone: PRUNED
        ("old-runbook.md", "restricted", 4, 4),         # file gone: PRUNED, own dept
        ("report.md", "general", 5, 0),                 # an upload: no stamp, kept
        ("internal/system/status", "restricted", 4, 0), # generated record: kept
        ("mixed.md", "general", 6, 2),                  # mixed: reported, kept
        ("docs/GONE.md", "restricted", 3, 3),           # docs/ is the docs prune's
    ))
    out = ingest_mod._prune_orphan_knowledge()
    assert sorted(deletions) == [("old-runbook.md", "restricted"),
                                 ("retired-guide.md", "general")]
    assert {d["source"] for d in out["deleted"]} == {"retired-guide.md", "old-runbook.md"}
    assert [m["source"] for m in out["mixed"]] == ["mixed.md"]


def test_a_missing_or_empty_knowledge_dir_prunes_nothing(tmp_path, deletions, monkeypatch):
    rows = _rows(("faq.md", "general", 3, 3))
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: rows)
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(tmp_path / "not-mounted"))
    assert ingest_mod._prune_orphan_knowledge()["skipped"] == "knowledge dir missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "diagram.png").write_bytes(b"not a watched type")
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(empty))
    assert "no eligible file" in ingest_mod._prune_orphan_knowledge()["skipped"]
    assert ingest_mod._prune_orphan_knowledge(force=True)["deleted"] == []
    assert deletions == []


def test_more_candidates_than_the_cap_is_a_refusal_unless_forced(kdir, deletions, monkeypatch):
    many = _rows(*[(f"gone-{i}.md", "general", 1, 1)
                   for i in range(ingest_mod._KNOWLEDGE_PRUNE_CAP + 1)])
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: many)
    out = ingest_mod._prune_orphan_knowledge()
    assert deletions == [] and out["deleted"] == []
    assert "exceed the cap" in out["skipped"]
    assert len(out["would_delete"]) == ingest_mod._KNOWLEDGE_PRUNE_CAP + 1
    ingest_mod._prune_orphan_knowledge(force=True)
    assert len(deletions) == ingest_mod._KNOWLEDGE_PRUNE_CAP + 1


def test_the_cap_itself_is_not_a_refusal(kdir, deletions, monkeypatch):
    exactly = _rows(*[(f"gone-{i}.md", "general", 1, 1)
                      for i in range(ingest_mod._KNOWLEDGE_PRUNE_CAP)])
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: exactly)
    out = ingest_mod._prune_orphan_knowledge()
    assert "skipped" not in out
    assert len(deletions) == ingest_mod._KNOWLEDGE_PRUNE_CAP


def test_dry_run_removes_nothing(kdir, deletions, monkeypatch):
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: _rows(
        ("retired-guide.md", "general", 7, 7)))
    out = ingest_mod._prune_orphan_knowledge(dry_run=True)
    assert deletions == [] and out["deleted"] == []
    assert out["would_delete"] == [{"source": "retired-guide.md",
                                    "department": "general", "count": 7}]


def test_a_file_written_back_before_the_delete_is_kept(kdir, deletions, monkeypatch):
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: _rows(
        ("back.md", "general", 2, 2), ("gone.md", "general", 2, 2)))
    real_lock = ingest_mod._source_lock

    def lock_then_restore(source):
        if source == "back.md":
            (kdir / "back.md").write_text("# Back\n", encoding="utf-8")  # the sync won the race
        return real_lock(source)

    monkeypatch.setattr(ingest_mod, "_source_lock", lock_then_restore)
    out = ingest_mod._prune_orphan_knowledge()
    assert deletions == [("gone.md", "general")]
    assert [d["source"] for d in out["deleted"]] == ["gone.md"]


def test_the_knowledge_sync_reports_what_it_pruned(kdir, deletions, monkeypatch):
    monkeypatch.setattr(ingest_mod, "_ingest_file", lambda name, text: 1)
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: _rows(
        ("faq.md", "general", 1, 1), ("retired-guide.md", "general", 7, 7)))
    res = ingest_mod._sync_knowledge_dir(force=True)
    assert res["retired-guide.md"] == {"status": "pruned", "chunks": 7}
    assert ("retired-guide.md", "general") in deletions


def test_a_refused_or_failed_prune_is_reported_not_swallowed(kdir, deletions, monkeypatch):
    monkeypatch.setattr(ingest_mod, "_ingest_file", lambda name, text: 1)
    many = _rows(*[(f"gone-{i}.md", "general", 1, 1)
                   for i in range(ingest_mod._KNOWLEDGE_PRUNE_CAP + 1)])
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: many)
    res = ingest_mod._sync_knowledge_dir(force=True)
    assert res["knowledge/_prune"]["status"] == "skipped"
    assert "exceed the cap" in res["knowledge/_prune"]["reason"]
    assert deletions == []

    def unreadable():
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(ingest_mod, "list_file_sources", unreadable)
    res = ingest_mod._sync_knowledge_dir(force=True)
    assert res["knowledge/_prune"] == {"status": "error", "error": "index unavailable"}
    assert res["faq.md"]["status"] == "ok"   # the files still synced


def test_the_admin_endpoint_dry_runs_both_namespaces_and_is_owner_only(
        client, admin_headers, kdir, deletions, monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(ingest_mod, "_DOCS_DIR", docs)
    monkeypatch.setattr(ingest_mod, "_DOCS_ROOT_FILES", [])
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: _rows(
        ("retired-guide.md", "general", 23, 23)))
    monkeypatch.setattr(ingest_mod, "list_sources", lambda department=None: [
        {"source": "docs/GONE.md", "department": "restricted", "count": 2}])
    assert client.post("/api/admin/kb/prune-orphans?dry_run=true").status_code in (401, 403)
    r = client.post("/api/admin/kb/prune-orphans?dry_run=true", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert deletions == [] and body["deleted"] == []
    assert {w["source"] for w in body["would_delete"]} == {"docs/GONE.md", "retired-guide.md"}
    r = client.post("/api/admin/kb/prune-orphans", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert {d["source"] for d in r.json()["deleted"]} == {"docs/GONE.md", "retired-guide.md"}
    assert "would_delete" not in r.json()


def test_the_admin_endpoint_forces_past_the_cap(client, admin_headers, kdir, deletions,
                                                monkeypatch, tmp_path):
    monkeypatch.setattr(ingest_mod, "_DOCS_DIR", tmp_path / "no-docs")
    monkeypatch.setattr(ingest_mod, "_DOCS_ROOT_FILES", [])
    monkeypatch.setattr(ingest_mod, "list_sources", lambda department=None: [])
    many = _rows(*[(f"gone-{i}.md", "general", 1, 1)
                   for i in range(ingest_mod._KNOWLEDGE_PRUNE_CAP + 1)])
    monkeypatch.setattr(ingest_mod, "list_file_sources", lambda: many)
    body = client.post("/api/admin/kb/prune-orphans", headers=admin_headers).json()
    assert body["deleted"] == [] and "exceed the cap" in body["knowledge_skipped"]
    body = client.post("/api/admin/kb/prune-orphans?force=true", headers=admin_headers).json()
    assert len(body["deleted"]) == ingest_mod._KNOWLEDGE_PRUNE_CAP + 1


def test_list_file_sources_counts_the_stamp_against_the_store(index):
    """The helper against the metadata a store holds (the suite's global mock
    returns whatever it is told, which is what a realism pin exists to
    avoid)."""
    gen = index.create_collection(database.GLOBAL_COLLECTION)
    res = index.create_collection("kb_restricted")
    gen.add(ids=["a1", "a2", "u1", "m1", "m2"],
            documents=["a", "b", "upload", "x", "y"],
            metadatas=[{"source": "gone.md", "from_file": "true"},
                       {"source": "gone.md", "from_file": "true"},
                       {"source": "report.md"},
                       {"source": "mixed.md", "from_file": "true"},
                       {"source": "mixed.md"}],
            embeddings=[EMBEDDING] * 5)
    res.add(ids=["r1"], documents=["r"],
            metadatas=[{"source": "handbook/x.md", "from_file": "true"}],
            embeddings=[EMBEDDING])
    rows = {(r["source"], r["department"]): (r["count"], r["file_chunks"])
            for r in database.list_file_sources()}
    assert rows == {("gone.md", "general"): (2, 2), ("report.md", "general"): (1, 0),
                    ("mixed.md", "general"): (2, 1),
                    ("handbook/x.md", "restricted"): (1, 1)}


# -- the FIX cases named by the defect ------------------------------------------

def test_help_pages_are_never_candidates(kdir, index):
    """The help sync stamps its pages from_file="true" too (app/help_docs.py),
    and none of them is a file under KNOWLEDGE_DIR. Listed, every help page
    would be a prune candidate on every boot. list_file_sources enumerates
    through _is_corpus_collection, which leaves kb_help out."""
    helpc = index.create_collection(database.HELP_COLLECTION)
    helpc.add(ids=["h1", "h2"], documents=["Getting started.", "Signing in."],
              metadatas=[{"source": "getting-started.md", "chunk": i,
                          "from_file": "true", "help_page": "true"} for i in range(2)],
              embeddings=[EMBEDDING] * 2)
    gen = index.create_collection(database.GLOBAL_COLLECTION)
    gen.add(ids=["g1"], documents=["A retired guide."],
            metadatas=[{"source": "retired-guide.md", "chunk": 0, "from_file": "true"}],
            embeddings=[EMBEDDING])
    plan = ingest_mod._prune_orphan_knowledge(dry_run=True)
    assert [w["source"] for w in plan["would_delete"]] == ["retired-guide.md"]
    assert all(r["department"] != database.HELP_DEPARTMENT
               for r in database.list_file_sources())
    ingest_mod._prune_orphan_knowledge()
    assert _sources(helpc) == {"getting-started.md": 2}
    assert _sources(gen) == {}


def test_api_ingest_drops_a_claimed_from_file(client, admin_headers):
    """from_file now decides deletion: a source whose every chunk carries it
    is pruned once no knowledge file of that name exists. A caller who could
    set it could put an API-ingested document up for deletion at the next
    boot."""
    from unittest.mock import patch
    captured = {}

    def _capture(doc_id, text, metadata=None, **kw):
        captured.update(metadata or {})
        return 1

    with patch("app.routers.kb.add_document", side_effect=_capture):
        r = client.post("/api/ingest",
                        json={"doc_id": "stamp-probe", "text": "Harmless text.",
                              "department": "general",
                              "metadata": {"source": "stamp-probe.md",
                                           "from_file": "true"}},
                        headers=admin_headers)
    assert r.status_code == 200, r.text
    assert captured.get("source") == "stamp-probe.md"
    assert "from_file" not in captured, "a caller claimed the file sync's stamp"


def test_a_knowledge_file_deleted_while_down_is_pruned_by_the_startup_sync(
        tmp_path, monkeypatch, index):
    """End to end: two files synced, one removed with no watcher running, then
    the startup sync (force=False) - the removed file's chunks and its
    fingerprint go; the other file is untouched.

    Fake store only. Through this file's patched seams, the full ingest path on
    real in-memory chroma raises StopIteration inside chroma's own segment
    lookup (a collection with no usable segment) - a harness interaction, not
    the prune: production ingests through the same calls, and the real-store
    behavior the prune depends on is pinned by this file's other real-chroma
    variants."""
    if not isinstance(index, _FakeClient):
        pytest.skip("the real-chroma ingest path trips a harness-only StopIteration "
                    "in chroma's segment lookup; see the docstring")
    k = tmp_path / "knowledge"
    k.mkdir()
    (k / "returns.md").write_text("## Returns\n\nThirty days.\n\n## Refunds\n\nTo the card.\n",
                                  encoding="utf-8")
    (k / "shipping.md").write_text("## Shipping\n\nTwo to five days.\n", encoding="utf-8")
    state: dict = {}
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(k))
    monkeypatch.setattr(ingest_mod, "_load_ingest_state", lambda: dict(state))
    monkeypatch.setattr(ingest_mod, "_save_ingest_state",
                        lambda s: (state.clear(), state.update(s)))

    first = ingest_mod._sync_knowledge_dir(force=False)
    assert first["returns.md"]["status"] == "ok" and first["shipping.md"]["status"] == "ok", first
    gen = index.get_collection(database.GLOBAL_COLLECTION)
    assert set(_sources(gen)) == {"returns.md", "shipping.md"}

    (k / "returns.md").unlink()   # deleted while the backend was down
    second = ingest_mod._sync_knowledge_dir(force=False)
    assert second["returns.md"]["status"] == "pruned", second
    assert second["shipping.md"]["status"] == "skipped"
    assert _sources(gen) == {"shipping.md": 1}
    assert set(state) == {"shipping.md"}


def test_an_upload_that_replaces_a_candidate_under_the_lock_is_kept(kdir, index, monkeypatch):
    """The candidate list is taken before any lock. An upload named like a
    deleted knowledge file takes the same source lock, replaces the file-owned
    chunks with its own unstamped ones and writes no knowledge file - so a
    source that read as an orphan at the listing is an upload by the time the
    prune holds its lock, and the disk re-check cannot tell the two apart.
    Pinned here (2026-10-29): under the lock the prune re-reads provenance and
    skips a source that is no longer wholly file-owned, logging the skip; the
    upload's chunks stay. The plain orphan beside it still prunes (the
    control). On the code before the re-read the upload was deleted whole."""
    gen = index.create_collection(database.GLOBAL_COLLECTION)
    gen.add(ids=["f1", "f2", "p1"],
            documents=["The old file.", "More of it.", "A retired guide."],
            metadatas=[{"source": "policy.md", "chunk": 0, "from_file": "true"},
                       {"source": "policy.md", "chunk": 1, "from_file": "true"},
                       {"source": "retired-guide.md", "chunk": 0, "from_file": "true"}],
            embeddings=[EMBEDDING] * 3)
    monkeypatch.setattr(database, "_embed", lambda text, **kw: EMBEDDING)
    events: list = []
    monkeypatch.setattr(ingest_mod, "log", lambda event, **kw: events.append((event, kw)))
    uploaded = ["Uploaded policy.", "A new section."]
    upload_ids = [hashlib.md5(f"general::policy.md::{c}".encode(),
                              usedforsecurity=False).hexdigest() for c in uploaded]
    real_lock = ingest_mod._source_lock
    raced: list = []

    def upload_then_lock(source):
        if source == "policy.md" and not raced:
            raced.append(source)
            # The upload route's write (routers/kb.py _ingest_upload), under
            # its own hold of this lock: the set diff against what is indexed,
            # the new chunks added with no from_file stamp, the stale ones -
            # here every file-owned chunk - deleted last.
            with real_lock(source):
                existing = set(database.get_source_ids(source, "general"))
                for i, (doc_id, chunk) in enumerate(zip(upload_ids, uploaded)):
                    if doc_id not in existing:
                        database.add_document(doc_id, chunk,
                                              {"source": source, "chunk": i, "trust": "curated"},
                                              department="general")
                stale = sorted(existing - set(upload_ids))
                if stale:
                    database.delete_documents(stale, "general")
        return real_lock(source)

    monkeypatch.setattr(ingest_mod, "_source_lock", upload_then_lock)
    out = ingest_mod._prune_orphan_knowledge()
    assert raced == ["policy.md"]
    assert [d["source"] for d in out["deleted"]] == ["retired-guide.md"]
    left = gen.get(where={"source": "policy.md"}, include=["metadatas"])
    assert sorted(left["ids"]) == sorted(upload_ids), "the prune deleted the upload"
    assert all("from_file" not in m for m in left["metadatas"])
    assert _sources(gen) == {"policy.md": 2}
    assert [kw["file"] for e, kw in events if e == "kb_orphan_pruned"] == ["retired-guide.md"]
    skipped = [kw for e, kw in events if e == "kb_orphan_prune_skipped"]
    assert [kw["file"] for kw in skipped] == ["policy.md"]
    assert (skipped[0]["count"], skipped[0]["file_chunks"]) == (2, 0)


@pytest.mark.parametrize("leg", ["sync", "queued"])
def test_an_upload_that_reuses_a_deleted_files_chunk_ids_owns_them(
        leg, tmp_path, monkeypatch, index, client, admin_headers):
    """Chunk ids are content-addressed with one formula for every writer, so an
    upload named like a deleted knowledge file, its text byte-identical to the
    file's, lands on the file's own ids. The upload's set diff skipped an id
    already indexed, and nothing rewrote the row: every chunk kept the file
    sync's from_file stamp, the source still read as wholly file-owned, and the
    next prune deleted the upload (the re-read under the lock cannot tell -
    the metadata says file-owned). Pinned here (2026-10-29): the upload
    re-stamps the ids it reuses with its own metadata, on both the synchronous
    upload and the queued worker, so the source reads file_chunks 0 and the
    prune keeps it. The plain orphan beside it still prunes (the control).

    The collection is created first, bare, as this file's other real-store
    cases do: the in-memory store refuses the production collection metadata
    on CREATE (its hnsw persistence keys are the persistent index's), and
    get_or_create on an existing collection does not re-validate them."""
    index.create_collection(database.GLOBAL_COLLECTION)
    k = tmp_path / "knowledge"
    k.mkdir()
    body = ("Returns are accepted within thirty days of delivery. Items must be "
            "unused and in the original packaging. Refunds go to the original "
            "card.\n") * 12
    (k / "policy.md").write_bytes(body.encode("utf-8"))
    (k / "retired-guide.md").write_bytes(b"A retired guide.\n")
    (k / "shipping.md").write_bytes(b"Two to five days.\n")
    state: dict = {}
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(k))
    monkeypatch.setattr(ingest_mod, "_load_ingest_state", lambda: dict(state))
    monkeypatch.setattr(ingest_mod, "_save_ingest_state",
                        lambda s: (state.clear(), state.update(s)))
    monkeypatch.setattr(database, "_embed", lambda text, **kw: EMBEDDING)

    first = ingest_mod._sync_knowledge_dir(force=False)
    assert {first[n]["status"] for n in ("policy.md", "retired-guide.md",
                                         "shipping.md")} == {"ok"}, first
    file_ids = set(database.get_source_ids("policy.md", "general"))
    assert len(file_ids) > 1
    (k / "policy.md").unlink()          # deleted while the backend was down
    (k / "retired-guide.md").unlink()   # the plain orphan

    if leg == "sync":
        r = client.post("/api/ingest/upload",
                        files={"file": ("policy.md", body.encode("utf-8"), "text/markdown")},
                        data={"department": "general"}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ingested", r.json()
    else:
        from app import jobs
        job_id = jobs.create_job(source="policy.md", department="general")
        # Untrusted on this leg, so the upload's own tier is visible on the
        # rows it reused (the file sync stamped them curated).
        jobs._run_ingest(job_id, "policy.md", body, "general", {"trust": "untrusted"})
        assert next(j for j in jobs.list_jobs()
                    if j["job_id"] == job_id)["status"] == "complete"
    # The same ids: the upload reused every one of them, nothing re-embedded.
    assert set(database.get_source_ids("policy.md", "general")) == file_ids
    owned = {(r["source"], r["department"]): (r["count"], r["file_chunks"])
             for r in database.list_file_sources()}

    second = ingest_mod._sync_knowledge_dir(force=False)
    assert second["retired-guide.md"]["status"] == "pruned", second
    assert "policy.md" not in second, "the prune deleted the upload"
    gen = index.get_collection(database.GLOBAL_COLLECTION)
    left = gen.get(where={"source": "policy.md"}, include=["metadatas"])
    assert set(left["ids"]) == file_ids, "the prune deleted the upload"
    assert owned[("policy.md", "general")] == (len(file_ids), 0)
    upload_tier = "curated" if leg == "sync" else "untrusted"
    assert all(m.get("from_file") != "true" and m["trust"] == upload_tier
               for m in left["metadatas"]), left["metadatas"]
    assert _sources(gen) == {"policy.md": len(file_ids), "shipping.md": 1}


# -- CONTROLs: pass before the prune existed and after ---------------------------

def test_control_an_upload_named_like_a_deleted_knowledge_file_survives(
        tmp_path, monkeypatch, index):
    """Uploads are keyed by their file name - the shape of a knowledge key -
    and carry no from_file stamp. Neither an upload alone nor an upload that
    shares a name with a file-synced source is the prune's to delete."""
    k = tmp_path / "knowledge"
    k.mkdir()
    (k / "shipping.md").write_text("## Shipping\n\nTwo to five days.\n", encoding="utf-8")
    gen = index.create_collection(database.GLOBAL_COLLECTION,
                                        metadata={"hnsw:space": "cosine"})
    gen.add(ids=["up1", "up2", "mix-up", "mix-file"],
            documents=["Uploaded policy.", "More of it.", "An uploaded faq.", "The faq file."],
            metadatas=[{"source": "policy.md", "chunk": 0, "trust": "curated"},
                       {"source": "policy.md", "chunk": 1, "trust": "curated"},
                       {"source": "faq.md", "chunk": 0, "trust": "curated"},
                       {"source": "faq.md", "chunk": 0, "from_file": "true"}],
            embeddings=[EMBEDDING] * 4)
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(k))
    monkeypatch.setattr(ingest_mod, "_ingest_file", lambda name, text: 1)
    monkeypatch.setattr(ingest_mod, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_mod, "_save_ingest_state", lambda s: None)
    ingest_mod._sync_knowledge_dir(force=True)
    left = _sources(gen)
    assert left.get("policy.md") == 2, "the sync deleted an upload"
    assert left.get("faq.md") == 2, "the sync deleted part of a mixed source"


def test_control_the_docs_prune_is_unchanged(tmp_path, monkeypatch):
    """Called as _sync_docs calls it - no arguments - the docs prune deletes
    exactly the docs/ sources with no file, leaves every other key alone, and
    answers in the same shape."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "keep.md").write_text("# Keep\n", encoding="utf-8")
    monkeypatch.setattr(ingest_mod, "_DOCS_DIR", docs)
    monkeypatch.setattr(ingest_mod, "_DOCS_ROOT_FILES", [])
    monkeypatch.setattr(ingest_mod, "list_sources", lambda department=None: [
        {"source": "docs/keep.md", "department": "general", "count": 1},
        {"source": "docs/gone.md", "department": "restricted", "count": 2},
        {"source": "gone-knowledge.md", "department": "general", "count": 3}])
    deleted = []
    monkeypatch.setattr(ingest_mod, "delete_source",
                        lambda source, dept=None: deleted.append((source, dept)))
    out = ingest_mod._prune_orphan_docs()
    assert deleted == [("docs/gone.md", "restricted")]
    assert set(out) == {"deleted", "docs_sources_in_index"}
    assert out["deleted"] == [{"source": "docs/gone.md", "department": "restricted", "count": 2}]
    assert [d["source"] for d in out["docs_sources_in_index"]] == ["docs/gone.md", "docs/keep.md"]


def test_control_the_legacy_basename_purge_still_uses_delete_file_chunks(
        tmp_path, monkeypatch):
    """A subdir file's old basename key is purged through delete_file_chunks
    (stamped chunks only), never delete_source - the prune that now follows
    the loop does not change which delete the purge reaches for."""
    k = tmp_path / "knowledge"
    (k / "handbook").mkdir(parents=True)
    (k / "handbook" / "b.md").write_text("handbook b", encoding="utf-8")
    purged, by_name = [], []
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(k))
    monkeypatch.setattr(ingest_mod, "_ingest_file", lambda name, text: 1)
    monkeypatch.setattr(ingest_mod, "delete_file_chunks",
                        lambda source, dept=None: purged.append(source))
    monkeypatch.setattr(ingest_mod, "delete_source",
                        lambda source, dept=None: by_name.append(source))
    # The index after the purge: nothing of the old key is left to prune.
    monkeypatch.setattr(ingest_mod, "list_file_sources",
                        lambda: _rows(("handbook/b.md", "general", 1, 1)), raising=False)
    monkeypatch.setattr(ingest_mod, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_mod, "_save_ingest_state", lambda s: None)
    ingest_mod._sync_knowledge_dir(force=True)
    assert purged == ["b.md"]
    assert by_name == []
