"""/api/ingest refuses an id a system writer holds - and the race in that check.

The direct-ingest route reads the id's stored metadata and refuses one stamped
from_file (the knowledge file sync) or auto_generated (the system records)
with a 409. The read and the write that follows are not serialized against
those writers: a writer that lands the id between the route's read and its
add_document has its row replaced by the caller's text, and the file sync
re-runs a file only when the file changes.

The ids are content-addressed (md5 of department, source and chunk text), so
a caller who knows a source's text can predict them, under any source name of
its own. The writers serialize per SOURCE (ingest_sync._source_lock, keyed by
the writer's source name); the system records take no lock; and nothing maps
a predicted id back to the writer's source. So no lock that exists today can
be taken by this route across the check and the write. Closing the race is a
design question (a per-id or store-level write lock that every writer of a
protected id takes), not a one-route change - the test is kept as an expected
failure until that design lands.

CONTROLS: an id already held by a writer is refused; an id the caller wrote
earlier is replaced, as this route's normal use.
"""
import pytest

from app.routers import kb

_OWNER = {"id": 1, "role": "owner", "department": "general"}
_FILE_ROW = ("Original file text",
             {"source": "policy.md", "chunk": 0, "from_file": "true", "trust": "curated"})


@pytest.fixture
def store(monkeypatch):
    """An in-memory id -> (text, metadata) map standing in for the collection."""
    rows: dict[str, tuple[str, dict]] = {}

    def _read(ids, department=None):
        return {i: dict(rows[i][1]) for i in ids if i in rows}

    def _add(doc_id, text, meta, department=None, **kw):
        rows[doc_id] = (text, dict(meta))

    monkeypatch.setattr(kb, "chunk_metadata", _read)
    monkeypatch.setattr(kb, "add_document", _add)
    return rows


def _send(doc_id: str, text: str = "Caller replacement"):
    request = kb.IngestRequest(doc_id=doc_id, text=text,
                               metadata={"source": "mine.md"}, department="general")
    return kb.ingest(request, _OWNER)


@pytest.mark.xfail(strict=True, reason=(
    "Design question: no lock is shared by every writer of a protected id. The "
    "file sync locks per its own source name, the system records take none, and "
    "a predicted id names no source this route could lock. When the design "
    "lands, the simulated writer below must take the same lock."))
def test_a_writer_that_lands_between_the_check_and_the_write_keeps_its_row(
        store, monkeypatch):
    doc_id = "predicted-content-id"
    real_read = kb.chunk_metadata

    def _read_then_writer_lands(ids, department=None):
        snapshot = real_read(ids, department)
        store[doc_id] = _FILE_ROW   # the file sync writes the id right after the read
        return snapshot

    monkeypatch.setattr(kb, "chunk_metadata", _read_then_writer_lands)
    try:
        _send(doc_id)
    except kb.HTTPException as e:
        assert e.status_code == 409
    assert store[doc_id] == _FILE_ROW


def test_control_an_id_already_held_by_a_writer_is_refused(store):
    store["held-id"] = _FILE_ROW
    with pytest.raises(kb.HTTPException) as refused:
        _send("held-id")
    assert refused.value.status_code == 409
    assert store["held-id"] == _FILE_ROW


def test_control_the_callers_own_id_is_replaced(store):
    _send("my-id", "first text")
    out = _send("my-id", "second text")
    assert out == {"status": "ingested", "doc_id": "my-id"}
    assert store["my-id"][0] == "second text"
    assert store["my-id"][1]["from_file"] == "false"
