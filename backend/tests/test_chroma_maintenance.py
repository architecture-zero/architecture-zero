"""Boot-path index maintenance, against a REAL persistent chroma.

This module runs on every boot and it deletes things - sqlite rows, on-disk
directories, and (only when triggered) whole collections. The global chromadb
mock cannot see any of the behaviour that makes that safe, so these tests build
a real persistent store in tmp_path instead.

The single most important test here is the negative sweep: the directory the
purge loop walks is the operator's DATA directory, which also holds the
relational database, the backup folder and the ingest state. The guard is three
conjoined conditions and it is correct - these prove it stays correct.

The client is built from the real Settings and Client classes directly, because
conftest patches chromadb.PersistentClient in the chromadb namespace. Every test
passes client= AND chroma_path= explicitly: the module's default client is that
same mock, whose list_collections returns [], so a test that forgot would pass
while exercising nothing.
"""
import json
import os
import sqlite3

import pytest

from chromadb.config import Settings as _RealSettings
from chromadb.api.client import Client as _RealClient
from chromadb.api.shared_system_client import SharedSystemClient

import app.chroma_maintenance as cm

DIM = 8


def _vec(seed: float) -> list[float]:
    """Exact in float32, so a round-trip comparison can be equality rather than
    a tolerance - the point of the preservation test is that nothing changed."""
    return [seed + i * 0.25 for i in range(DIM)]


@pytest.fixture
def store(tmp_path):
    """(client, path) over a real persistent chroma rooted at tmp_path."""
    path = str(tmp_path / "data")
    os.makedirs(path, exist_ok=True)
    client = _RealClient(settings=_RealSettings(
        is_persistent=True, persist_directory=path, anonymized_telemetry=False))
    yield client, path
    SharedSystemClient.clear_system_cache()


def _seed(client, name="kb_live", n=3):
    col = client.get_or_create_collection(
        name=name, metadata={"hnsw:space": "cosine",
                             "hnsw:sync_threshold": 50, "hnsw:batch_size": 25})
    col.add(ids=[f"id{i}" for i in range(n)],
            documents=[f"doc {i}" for i in range(n)],
            embeddings=[_vec(i) for i in range(n)],
            metadatas=[{"source": "faq.md", "from_file": "true"} for _ in range(n)])
    return col


def _noop(_sources):
    pass


def _counts(path):
    con = sqlite3.connect(os.path.join(path, "chroma.sqlite3"))
    try:
        return {t: con.execute(f"select count(*) from {t}").fetchone()[0]
                for t in ("collections", "segments", "embeddings",
                          "embedding_metadata", "max_seq_id")}
    finally:
        con.close()


# -- The purge loop runs in the operator's data directory ---------------------

def test_the_sweep_never_touches_a_neighbour_in_the_data_directory(store):
    """THE test in this file. CHROMA_PATH and DATA_DIR are the same directory,
    so this loop walks past the relational database, the backup folder and the
    ingest state on its way to orphaned vector dirs. Three conditions keep it
    honest: it must be a directory, its name must be EXACTLY a UUID, and no
    live segment may claim it."""
    client, path = store
    _seed(client)

    os.makedirs(os.path.join(path, "backups"), exist_ok=True)
    os.makedirs(os.path.join(path, "not-a-segment"), exist_ok=True)
    # A UUID-shaped name with a suffix must NOT match - the guard uses
    # fullmatch, and a search would delete this one.
    os.makedirs(os.path.join(path, "12345678-1234-1234-1234-123456789abc.bak"),
                exist_ok=True)
    for f in ("history.db", "ingest-state.json", "rag_metrics.json",
              "boot-history.json"):
        with open(os.path.join(path, f), "w", encoding="utf-8") as fh:
            fh.write("{}")

    cm.purge_orphan_segments(chroma_path=path)

    survivors = set(os.listdir(path))
    for expected in ("backups", "not-a-segment", "history.db",
                     "ingest-state.json", "rag_metrics.json",
                     "boot-history.json", "chroma.sqlite3",
                     "12345678-1234-1234-1234-123456789abc.bak"):
        assert expected in survivors, f"the sweep destroyed {expected}"


def test_the_on_disk_dir_name_is_the_vector_segment_id(store):
    """The sweep's live-segment check is only meaningful because the directory
    name IS the vector segment's id. Nothing else in the codebase asserts that
    identity - and if a future version keyed directories by COLLECTION id
    instead, every LIVE index would look orphaned and the loop would delete all
    of them, silently, at boot, with every other test in this file still
    passing."""
    client, path = store
    _seed(client)
    con = sqlite3.connect(os.path.join(path, "chroma.sqlite3"))
    try:
        vector_segs = {str(r[0]) for r in con.execute(
            "select id from segments where scope = 'VECTOR'")}
    finally:
        con.close()
    dirs = {e for e in os.listdir(path)
            if os.path.isdir(os.path.join(path, e)) and cm._UUID_RE.fullmatch(e)}
    assert dirs, "a live collection must have an on-disk vector dir"
    assert dirs <= vector_segs, (
        "an on-disk UUID dir is not a live VECTOR segment id - the sweep's "
        "orphan predicate no longer means what it says")


def test_the_sweep_collects_what_delete_collection_strands(store):
    client, path = store
    _seed(client, "kb_live")
    _seed(client, "kb_doomed")
    client.delete_collection("kb_doomed")

    stranded = _counts(path)
    assert stranded["embeddings"] > 3, "delete_collection should strand rows"

    out = cm.purge_orphan_segments(chroma_path=path)
    after = _counts(path)
    assert out["embedding_rows"] > 0 or out["dirs"] > 0
    # The surviving collection is untouched and still queryable.
    live = client.get_collection("kb_live")
    assert live.count() == 3
    assert live.query(query_embeddings=[_vec(0)], n_results=1)["ids"][0]
    assert after["collections"] == 1


def test_a_missing_store_is_a_no_op(tmp_path):
    """First boot of a fresh clone: no database, nothing to do, no exception."""
    empty = str(tmp_path / "nothing")
    os.makedirs(empty)
    assert cm.purge_orphan_segments(chroma_path=empty) == {
        "segments": 0, "embedding_rows": 0, "dirs": 0}


# -- What does and does not trigger a rebuild ---------------------------------

def test_parameter_drift_is_reported_and_never_acted_on(store, caplog):
    """The design decision this pins: adopting new index parameters means
    dropping and re-adding a HEALTHY collection, with the only copy of its
    records in memory until the re-add finishes. It is reported so an operator
    can decide, and the force-rebuild flag is how they say yes."""
    client, path = store
    col = client.get_or_create_collection(
        name="kb_drift", metadata={"hnsw:space": "cosine",
                                   "hnsw:sync_threshold": 999})
    col.add(ids=["a"], documents=["x"], embeddings=[_vec(1)],
            metadatas=[{"source": "faq.md", "from_file": "true"}])

    with caplog.at_level("ERROR"):
        summary = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)

    res = summary["collections"]["kb_drift"]
    assert res["params_ok"] is False
    assert res["params_drift"] is True
    assert res["rebuilt"] is False, "drift must NOT trigger an automatic rebuild"
    assert client.get_collection("kb_drift").count() == 1
    assert "NOT rebuilding automatically" in caplog.text


def test_a_halted_export_forces_the_rebuild_path(store, monkeypatch):
    """A halted export means the index errored mid-read: it is corrupt. The
    in-place delete must never run against it - deleting from a corrupt index
    can crash the process natively, which a supervisor turns into a restart
    loop. The rebuild path never touches the sick graph."""
    client, path = store
    _seed(client, "kb_sick")
    deleted = []
    real_get = client.get_collection

    def _watch(name=None, **kw):
        col = real_get(name=name, **kw)
        orig_delete = col.delete
        col.delete = lambda *a, **k: deleted.append(a) or orig_delete(*a, **k)
        return col

    monkeypatch.setattr(client, "get_collection", _watch)
    monkeypatch.setattr(cm, "_export_embeddings", lambda col: ({}, True))

    res = cm._maintain_collection("kb_sick", {"hnsw:space": "cosine"},
                                  _noop, client)
    assert res["halted"] is True
    assert res["rebuilt"] is True, "a corrupt index must take the rebuild path"
    assert deleted == [], "an in-place delete ran against a corrupt index"


def test_a_rebuild_preserves_records_and_vectors_exactly(store):
    client, path = store
    _seed(client, "kb_forced", n=4)
    with open(os.path.join(path, cm._FORCE_REBUILD_FILE), "w",
              encoding="utf-8") as f:
        json.dump(["kb_forced"], f)

    summary = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    res = summary["collections"]["kb_forced"]
    assert res["forced"] is True and res["rebuilt"] is True
    assert res["status"] == "ok"

    col = client.get_collection("kb_forced")
    assert col.count() == 4
    got = col.get(ids=["id2"], include=["embeddings", "documents", "metadatas"])
    assert [float(x) for x in got["embeddings"][0]] == _vec(2)
    assert got["documents"][0] == "doc 2"
    assert got["metadatas"][0]["source"] == "faq.md"
    # Retrievable, not merely present.
    assert col.query(query_embeddings=[_vec(2)], n_results=1)["ids"][0] == ["id2"]


def test_a_racing_re_mint_during_a_rebuild_does_not_lose_records(store, monkeypatch):
    """Routers serve while the boot task runs, so any read can re-mint a
    collection through get_or_create between the drop and the re-add. Using
    create_collection there raises a uniqueness error and the export is lost;
    get_or_create plus upsert adopts the racer's collection instead."""
    client, path = store
    _seed(client, "kb_race", n=3)
    real_delete = client.delete_collection

    def _delete_then_racer_remints(name):
        real_delete(name)
        # The racer: exactly what query_similar does on a cold read.
        client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine",
                                 "hnsw:sync_threshold": 50, "hnsw:batch_size": 25})

    monkeypatch.setattr(client, "delete_collection", _delete_then_racer_remints)
    res = cm._maintain_collection(
        "kb_race", {"hnsw:space": "cosine", "hnsw:sync_threshold": 50,
                    "hnsw:batch_size": 25}, _noop, client, force_rebuild=True)

    assert res["rebuilt"] is True
    assert res["status"] == "ok", res
    assert client.get_collection("kb_race").count() == 3, "records were lost to the race"


def test_maintenance_is_idempotent(store):
    client, path = store
    _seed(client, "kb_idem")
    first = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    second = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    for summary in (first, second):
        res = summary["collections"]["kb_idem"]
        assert res["rebuilt"] is False and res["dead"] == 0
    assert client.get_collection("kb_idem").count() == 3


# -- The operator lever -------------------------------------------------------

def test_the_flag_fires_once_and_is_consumed(store):
    client, path = store
    _seed(client, "kb_once")
    flag = os.path.join(path, cm._FORCE_REBUILD_FILE)
    with open(flag, "w", encoding="utf-8") as f:
        json.dump(["kb_once"], f)

    first = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    assert first["collections"]["kb_once"]["rebuilt"] is True
    assert not os.path.exists(flag), "the flag must be consumed"

    second = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    assert second["collections"]["kb_once"]["rebuilt"] is False


def test_a_misspelled_collection_in_the_flag_is_loud_not_silent(store, caplog):
    """The flag is already consumed by the time the name is checked, so a typo
    means the sick collection stays sick. Saying so is the difference between
    an operator retrying and an operator waiting."""
    client, path = store
    _seed(client, "kb_real")
    with open(os.path.join(path, cm._FORCE_REBUILD_FILE), "w",
              encoding="utf-8") as f:
        json.dump(["kb_typo"], f)

    with caplog.at_level("ERROR"):
        cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    assert "unknown collection" in caplog.text
    assert "kb_typo" in caplog.text


def test_a_malformed_flag_is_consumed_and_ignored(store, caplog):
    client, path = store
    _seed(client, "kb_ok")
    flag = os.path.join(path, cm._FORCE_REBUILD_FILE)
    with open(flag, "w", encoding="utf-8") as f:
        f.write("{not json at all")

    with caplog.at_level("ERROR"):
        summary = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    assert not os.path.exists(flag)
    assert summary["collections"]["kb_ok"]["rebuilt"] is False
    assert "unreadable" in caplog.text


# -- Fail-open, never fail-silent ---------------------------------------------

def test_a_failure_to_list_collections_is_reported_not_raised(store):
    client, path = store

    class _Broken:
        def list_collections(self):
            raise RuntimeError("store unavailable")

    summary = cm.run_chroma_maintenance(_noop, client=_Broken(), chroma_path=path)
    assert "error" in summary and "list_collections failed" in summary["error"]


def test_one_collection_failing_does_not_stop_the_others(store, monkeypatch):
    client, path = store
    _seed(client, "kb_a")
    _seed(client, "kb_b")
    real = cm._maintain_collection

    def _boom(name, *a, **kw):
        if name == "kb_a":
            raise RuntimeError("segment read failed")
        return real(name, *a, **kw)

    monkeypatch.setattr(cm, "_maintain_collection", _boom)
    summary = cm.run_chroma_maintenance(_noop, client=client, chroma_path=path)
    assert summary["collections"]["kb_a"]["status"] == "error"
    assert summary["collections"]["kb_b"]["status"] == "ok"


def test_an_absent_store_disables_instance_eviction_rather_than_widening_it(tmp_path):
    """An empty live-segment set would evict EVERY cached instance. A missing
    database must disable a destructive action, never broaden it."""
    class _Client:
        class _server:
            class _manager:
                _instances = {"seg-1": object(), "seg-2": object()}

    empty = str(tmp_path / "gone")
    os.makedirs(empty)
    assert cm._drop_stale_segment_instances(_Client(), chroma_path=empty) == 0
    assert len(_Client._server._manager._instances) == 2


# -- Wiring -------------------------------------------------------------------

def test_maintenance_runs_before_the_ingest_syncs():
    """Ordering is load-bearing: this clears ingest fingerprints and drops dead
    records that the syncs then act on, and the knowledge sync snapshots its
    indexed-chunk counts at the top of its own run."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "main.py").read_text(encoding="utf-8")
    assert src.index("run_chroma_maintenance(") < src.index("_sync_knowledge_dir(force=False)")
