"""A kept row ends a write holding exactly the writer's metadata (2026-10-29).

Chunk ids are content-addressed (md5 of department, source and chunk text). A
caller who predicts a future chunk's EXACT text can write it first through
/api/ingest under the writer's own source, with keys of its own. The diffing
writers then KEEP that id - its stored text is theirs - and:
- the file sync, the upload, the queued upload and the release restamped it
  with the store's update, which MERGES metadata: the caller's entry_date
  (it demotes the chunk in retrieval) and, with the PII scan off, its
  pii_flagged stayed under the writer's stamp;
- the system-records writer did not restamp a kept id at all, so the caller's
  auto_generated "false" and tier stayed on the record's own text - the
  section lost its live-record label and status lift.

What these pin: after the writer's run the kept row's metadata is exactly
what the same writer wrote fresh for a sibling chunk (its own chunk index
aside), and the row keeps its stored vector - no embed call. An edit above a
section moves the kept rows' chunk index by a metadata update alone. The
CONTROL case passes on the code before the change too: a re-upload of an
unchanged document writes nothing.

Run against real in-memory chroma and against the fake store
(test_knowledge_orphan_prune's index fixture), as the restamp family is.
"""
import pytest

import app.database as database
import app.ingest_sync as ingest_mod
from app import corpus_scan
from app.chunking import chunk_markdown_sections, chunk_plain
from app.routers import kb
from tests.test_kept_id_own_text import (ATTACK, BODY, FILE_TEXT, NAME, _cid,
                                         _spy_writes_and_embeds)
from tests.test_knowledge_orphan_prune import EMBEDDING, index  # noqa: F401

# The pre-write's vector: not the writer's EMBEDDING, so a row that kept it
# provably was not re-embedded.
CALLER_VEC = [0.0, 1.0, 0.0, 0.0]
# Keys the caller sets and no writer writes: an entry_date demotes a chunk in
# retrieval; a pii_flagged lists the source among the PII sources.
CALLER_KEYS = {"entry_date": "2001-01-01", "pii_flagged": "true", "pii_types": "phone"}


@pytest.fixture
def store(index, monkeypatch):
    """The index, its global collection created bare first (the in-memory
    store refuses the production metadata on CREATE - the orphan prune file
    says why), the writer's fixed embedding, the ingest state off disk and
    the PII scan off - the case where the caller's pii_flagged survived."""
    index.create_collection(database.GLOBAL_COLLECTION)
    monkeypatch.setattr(database, "_embed", lambda text, **kw: EMBEDDING)
    monkeypatch.setattr(ingest_mod, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_mod, "_save_ingest_state", lambda s: None)
    monkeypatch.setattr(kb, "PII_SCAN_MODE", "off")
    return index


def _row(col, doc_id) -> tuple[str, dict, list[float]]:
    got = col.get(ids=[doc_id], include=["documents", "metadatas", "embeddings"])
    assert got["ids"] == [doc_id], got["ids"]
    return got["documents"][0], dict(got["metadatas"][0]), [float(x) for x in got["embeddings"][0]]


def _metas(col, source) -> dict[str, tuple[str, dict]]:
    got = col.get(where={"source": source}, include=["documents", "metadatas"])
    return {i: (d, dict(m)) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])}


def _caller_writes_first(client, headers, monkeypatch, doc_id, text,
                         department="general", source=NAME):
    """The exact-text pre-write: the writer's own text and source under an
    id no row holds yet, with the caller's keys, embedded as CALLER_VEC."""
    monkeypatch.setattr(database, "_embed", lambda t, **kw: CALLER_VEC)
    r = client.post("/api/ingest",
                    json={"doc_id": doc_id, "text": text, "department": department,
                          "metadata": {"source": source, "trust": "untrusted",
                                       **CALLER_KEYS}},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ingested", r.json()
    monkeypatch.setattr(database, "_embed", lambda t, **kw: EMBEDDING)


def _as_written_fresh(sibling_meta: dict, chunk: int) -> dict:
    return {**sibling_meta, "chunk": chunk}


# -- FIX ------------------------------------------------------------------------

def test_an_exact_text_prewrite_under_the_files_source_holds_the_files_metadata_alone(
        store, client, admin_headers, monkeypatch):
    """The file's second section is written first, word for word, with an
    entry_date and a PII tag. After the sync the row is the file's: the same
    keys and values as the section the sync wrote fresh, no caller key, and
    the stored vector kept - the sync embedded nothing."""
    parts = chunk_markdown_sections(FILE_TEXT)
    assert len(parts) == 2 and chunk_markdown_sections(parts[0]) == [parts[0]]
    assert ingest_mod._ingest_file(NAME, parts[0]) == 1
    _caller_writes_first(client, admin_headers, monkeypatch, _cid(parts[1]), parts[1])
    gen = store.get_collection(database.GLOBAL_COLLECTION)
    assert all(_row(gen, _cid(parts[1]))[1][k] == v for k, v in CALLER_KEYS.items())

    spy, embeds = _spy_writes_and_embeds(monkeypatch, gen)
    assert ingest_mod._ingest_file(NAME, FILE_TEXT) == 2
    assert embeds == [], embeds
    _, fresh, _ = _row(gen, _cid(parts[0]))
    text, meta, vec = _row(gen, _cid(parts[1]))
    assert text == parts[1]
    assert meta == _as_written_fresh(fresh, 1), meta
    assert meta["from_file"] == "true" and meta["trust"] == "curated", meta
    assert vec == CALLER_VEC, "the kept row was re-embedded"


@pytest.mark.parametrize("writer", ["upload", "queued", "release"])
def test_an_exact_text_prewrite_holds_the_upload_or_release_metadata_alone(
        writer, store, client, admin_headers, monkeypatch):
    """The same pre-write ahead of the other restamp callers: the upload (the
    synchronous route and the queued worker run in-line) and the Owner's
    quarantine release. Each merged its metadata over the caller's row."""
    gen = store.get_collection(database.GLOBAL_COLLECTION)
    chunk0 = chunk_plain(BODY)[0]
    kept = _cid(chunk0)
    qid = None
    if writer == "release":
        from app.quarantine import write_quarantine_row
        held = BODY + ATTACK
        assert chunk_plain(held)[0] == chunk0
        qid = write_quarantine_row(NAME, "general", "untrusted", held,
                                   corpus_scan.scan(held))["quarantine_id"]
    _caller_writes_first(client, admin_headers, monkeypatch, kept, chunk0)
    try:
        if writer == "release":
            r = client.post(f"/api/admin/kb/quarantine/{qid}/release",
                            headers=admin_headers)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "released", r.json()
        else:
            if writer == "queued":
                from app import jobs
                monkeypatch.setattr(jobs, "async_enabled", lambda: True)
                monkeypatch.setattr(
                    jobs, "dispatch_ingest",
                    lambda job_id, name, text, dept, extra_meta=None:
                        jobs._run_ingest(job_id, name, text, dept, extra_meta))
            r = client.post("/api/ingest/upload",
                            files={"file": (NAME, BODY.encode("utf-8"), "text/markdown")},
                            data={"department": "general"}, headers=admin_headers)
            assert r.status_code == 200, r.text
            assert r.json()["status"] in ("ingested", "queued"), r.json()
        if writer == "release":
            # Every other chunk of the held text carries the attack line, and
            # the gate types a hot chunk by its own findings, so the release's
            # metadata for a clean chunk is spelled out: its tags, its tier.
            expected = {"source": NAME, "chunk": 0, "trust": "untrusted",
                        "injection_flagged": "true",
                        "injection_types": corpus_scan.finding_types(
                            corpus_scan.scan(BODY + ATTACK))}
        else:
            sibling = next(m for i, (_, m) in _metas(gen, NAME).items() if i != kept)
            expected = _as_written_fresh(sibling, 0)
        text, meta, vec = _row(gen, kept)
        assert text == chunk0
        assert meta == expected, meta
        assert not set(CALLER_KEYS) & set(meta), meta
        assert vec == CALLER_VEC, "the kept row was re-embedded"
    finally:
        if qid is not None:
            from app.db import get_session
            from app.models import QuarantinedDoc
            with get_session() as db:
                row = db.get(QuarantinedDoc, qid)
                if row:
                    db.delete(row)


def test_an_exact_text_prewrite_of_a_record_section_carries_the_records_stamp_after_its_run(
        index, client, admin_headers, monkeypatch):
    """The generator kept the id and wrote nothing to it, so the record's own
    text sat under the caller's auto_generated "false" and tier - not
    labeled as a live record, not lifted on status questions. Its next run
    gives the row the record's keys, as it writes them fresh, and keeps the
    stored vector."""
    from app import system_records as sr
    dept = sr.DEPARTMENT
    col = index.create_collection(database._collection_name(dept))
    monkeypatch.setattr(database, "_embed", lambda text, **kw: EMBEDDING)
    monkeypatch.setattr(kb, "PII_SCAN_MODE", "off")
    v1 = "# Title\n\n## One\n\nAlpha line.\n"
    v2 = v1 + "\n## Two\n\nBeta line.\n"
    chunks = chunk_markdown_sections(v2)
    assert chunk_markdown_sections(v1) == chunks[:-1]
    assert sr._ingest(sr.POSTURE_SOURCE, v1)["status"] == "ok"
    future = _cid(chunks[-1], name=sr.POSTURE_SOURCE, dept=dept)
    _caller_writes_first(client, admin_headers, monkeypatch, future, chunks[-1],
                         department=dept, source=sr.POSTURE_SOURCE)
    assert _row(col, future)[1]["auto_generated"] == "false"

    spy, embeds = _spy_writes_and_embeds(monkeypatch, col)
    res = sr._ingest(sr.POSTURE_SOURCE, v2)
    assert (res["status"], res["added"], res["removed"]) == ("ok", 0, 0), res
    assert embeds == [], embeds
    _, fresh, _ = _row(col, _cid(chunks[0], name=sr.POSTURE_SOURCE, dept=dept))
    text, meta, vec = _row(col, future)
    assert text == chunks[-1]
    assert meta == _as_written_fresh(fresh, len(chunks) - 1), meta
    assert meta["auto_generated"] == "true" and meta["trust"] == "system", meta
    assert vec == CALLER_VEC, "the kept row was re-embedded"


def test_an_edit_above_moves_the_kept_rows_chunk_index_by_update_alone(
        store, monkeypatch):
    """A section added at the top shifts every kept section's chunk index
    without changing its text. The row then differs from what the sync
    writes only in a value, so it is written by metadata update - no
    delete, no re-add, no embed of a kept row."""
    parts = chunk_markdown_sections(FILE_TEXT)
    assert ingest_mod._ingest_file(NAME, FILE_TEXT) == 2
    gen = store.get_collection(database.GLOBAL_COLLECTION)
    spy, embeds = _spy_writes_and_embeds(monkeypatch, gen)
    edited = "## Intro\n\nA new first section.\n\n" + FILE_TEXT
    assert ingest_mod._ingest_file(NAME, edited) == 3
    assert embeds == [1], embeds   # the new section alone
    assert ("update", 2) in spy.writes, spy.writes
    assert ("delete", 2) not in spy.writes, spy.writes
    assert [_row(gen, _cid(p))[1]["chunk"] for p in parts] == [1, 2]


# -- CONTROL: pass before the change and after ------------------------------------

def test_control_a_reupload_of_an_unchanged_document_writes_nothing(
        store, client, admin_headers, monkeypatch):
    def upload():
        r = client.post("/api/ingest/upload",
                        files={"file": (NAME, BODY.encode("utf-8"), "text/markdown")},
                        data={"department": "general"}, headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ingested", r.json()

    monkeypatch.setattr(kb, "PII_SCAN_MODE", "tag")
    upload()
    gen = store.get_collection(database.GLOBAL_COLLECTION)
    before = _metas(gen, NAME)
    spy, embeds = _spy_writes_and_embeds(monkeypatch, gen)
    upload()
    assert spy.writes == [] and embeds == [], (spy.writes, embeds)
    assert _metas(gen, NAME) == before
