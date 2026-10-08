"""An empty vector store is an error while KNOWLEDGE_DIR holds documents (2026-10-08).

THE GAP (filed by the R-AZ-01 read). The startup hook called the retrieval-lane
probe without corpus_expected, because a client deployment starts empty - so
an empty store read "not_required" and readiness passed even beside documents
the instance should have been serving: a store volume not mounted, an index
wiped, ingestion failing. Now a KNOWLEDGE_DIR holding a document the startup
sync would ingest makes the corpus expected, and an empty or missing one keeps
"not_required".
"""
import asyncio
import types

import pytest

import app.database as db
import app.ingest_sync as ingest_sync


@pytest.fixture
def kd(tmp_path, monkeypatch):
    path = tmp_path / "knowledge"
    path.mkdir()
    monkeypatch.setattr(ingest_sync, "KNOWLEDGE_DIR", str(path))
    return path


def _files_the_sync_skips(kd):
    """A suffix it does not watch, and a watched one with blank text."""
    (kd / "diagram.png").write_bytes(b"\x89PNG not text")
    (kd / "contract.docx").write_bytes(b"PK zipped")
    (kd / "blank.md").write_text("  \n\t\n", encoding="utf-8")


def test_a_missing_directory_expects_no_corpus(kd, monkeypatch):
    monkeypatch.setattr(ingest_sync, "KNOWLEDGE_DIR", str(kd / "not-mounted"))
    assert ingest_sync._knowledge_dir_has_documents() is False


@pytest.mark.parametrize("tree, expected", [
    ("empty", False), ("skipped_only", False), ("deep_document", True)])
def test_the_rule_is_the_syncs(kd, monkeypatch, tree, expected):
    """True exactly when _sync_knowledge_dir would ingest something."""
    if tree != "empty":
        _files_the_sync_skips(kd)
    if tree == "deep_document":
        deep = kd / "company" / "policies"
        deep.mkdir(parents=True)
        (deep / "leave.md").write_text("# Leave\nTwenty days a year.", encoding="utf-8")
    ingested = []
    monkeypatch.setattr(ingest_sync, "_ingest_file",
                        lambda name, text: ingested.append(name) or 1)
    monkeypatch.setattr(ingest_sync, "delete_source", lambda source, dept=None: None)
    monkeypatch.setattr(ingest_sync, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_sync, "_save_ingest_state", lambda state: None)
    ingest_sync._sync_knowledge_dir(force=True)
    assert bool(ingested) is expected
    assert ingest_sync._knowledge_dir_has_documents() is expected


def test_text_after_a_long_blank_run_still_counts(kd):
    # The read stops at the first non-blank text, so it reads in pieces - and
    # text past the first piece is text, as it is to the sync.
    (kd / "notes.txt").write_text(" " * 20_000 + "found", encoding="utf-8")
    assert ingest_sync._knowledge_dir_has_documents() is True


def test_the_walk_is_bounded(kd, monkeypatch):
    """It runs on every self-check pass. Past the limit it stops looking and
    counts the tree as holding documents: not required is found by a complete
    look, never assumed."""
    for i in range(5):
        (kd / f"image-{i}.png").write_bytes(b"x")
    monkeypatch.setattr(ingest_sync, "_DOCUMENT_SCAN_LIMIT", 3)
    assert ingest_sync._knowledge_dir_has_documents() is True
    monkeypatch.setattr(ingest_sync, "_DOCUMENT_SCAN_LIMIT", 10)
    assert ingest_sync._knowledge_dir_has_documents() is False


def test_a_directory_that_cannot_be_listed_counts_as_holding_documents(kd, monkeypatch):
    """pathlib's rglob swallowed a listing error and read a document in an
    unreadable directory as none (the 2026-10-08 read); os.walk reports it."""
    real_walk = ingest_sync.os.walk

    def _walk(top, onerror=None, **kw):
        onerror(PermissionError(13, "Permission denied", str(kd / "private")))
        yield from real_walk(top, onerror=onerror, **kw)
    monkeypatch.setattr(ingest_sync.os, "walk", _walk)
    assert ingest_sync._knowledge_dir_has_documents() is True


def test_a_walk_that_raises_counts_as_holding_documents(kd, monkeypatch):
    """A tree too deep to walk raised RecursionError, which the probe turned
    into probe_crashed and hid every other reason."""
    def _broken(top, onerror=None, **kw):
        raise RecursionError("too deep")
        yield  # pragma: no cover - makes this a generator, as os.walk is
    monkeypatch.setattr(ingest_sync.os, "walk", _broken)
    assert ingest_sync._knowledge_dir_has_documents() is True


def test_a_watched_file_that_cannot_be_read_counts(kd, monkeypatch):
    """It is the operator's document even when this process cannot read it."""
    (kd / "faq.md").write_text("How do I reset my password?", encoding="utf-8")

    def _open(path, *a, **k):
        raise PermissionError(13, "Permission denied", str(path))
    monkeypatch.setattr(ingest_sync, "open", _open, raising=False)
    assert ingest_sync._knowledge_dir_has_documents() is True


# -- The wiring: the probe the startup hook hands the timer -------------------

@pytest.fixture
def wired_lane_probe(monkeypatch):
    """The lane probe startup_tasks passes the self-check timer - captured, not
    re-typed, so these tests read the wiring itself. Nothing starts: the timer
    is a recorder, and the startup ingest's task is closed unrun."""
    from app import main, runtime_config, self_check
    wired = {}
    monkeypatch.setattr(self_check, "self_check_loop", lambda *a, **kw: wired.update(kw))

    def _no_task(coro):
        if asyncio.iscoroutine(coro):
            coro.close()
    monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(create_task=_no_task))
    monkeypatch.setattr(runtime_config, "_startup_ingest_active", False)
    asyncio.run(main.startup_tasks())
    # The vector store holds nothing.
    monkeypatch.setattr(db.client, "list_collections", lambda: [])
    return wired["rag_probe"]


def test_an_empty_store_beside_documents_is_an_error(kd, wired_lane_probe, monkeypatch):
    # The embed service answers (it is asked first since 2026-10-08), so the store is the reason.
    monkeypatch.setattr(db, "_embed", lambda *a, **k: [0.1] * 768)
    (kd / "faq.md").write_text("How do I reset my password?", encoding="utf-8")
    assert wired_lane_probe() == {"state": "error", "reason": "vector_store_empty"}


def test_an_empty_store_with_nothing_to_serve_is_not_required(kd, wired_lane_probe):
    """A client deployment that starts empty: unchanged, by design."""
    _files_the_sync_skips(kd)
    assert wired_lane_probe() == {"state": "not_required"}
