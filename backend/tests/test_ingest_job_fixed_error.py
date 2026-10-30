"""A failed queued ingest stores and serves a fixed sentence (2026-10-30).

The queued path wrote str(e) onto the job row - update_job(..., error=str(e))
in _run_ingest_locked, and "worker crashed: <e>" in the worker's last
handler - and GET /api/admin/jobs served the row as stored, so the Ingestion
Queue tab showed the store's or the library's own words (index file paths,
an object repr) under "show error". Its log line, ingest_async_error, kept
them whole. The rule for every surface: the row carries a fixed sentence, the
words go to the log bounded at 300.

FIX: a job whose ingest raises a long path-bearing error, and a job whose
worker crashes the same way, each store and serve their fixed sentence and
none of the words; the log keeps them cut to 300. CONTROL: a job that
succeeds stores no error, and a job held by the quarantine backstop keeps its
own fixed sentence with the quarantine id.
"""
import pytest

from app import jobs

WORDS = ("chroma upsert failed for /app/data/chroma/7f3a/data_level0.bin: "
         "database disk image is malformed <chromadb.api.models.Collection "
         "object at 0x7f3a2c>" + " x" * 1000)
PATH = "/app/data/chroma/7f3a"
FAILED = "The document could not be indexed. See the server log."
CRASHED = "The ingest worker crashed. See the server log."


@pytest.fixture(autouse=True)
def _reset_queue_depth():
    jobs._PENDING = 0
    yield
    jobs._PENDING = 0


def _record(monkeypatch) -> list:
    # The queued path imports log_error from app.logger at call time.
    from app import logger
    seen: list = []
    monkeypatch.setattr(logger, "log_error",
                        lambda event, **kw: seen.append((event, kw)))
    return seen


def _index(monkeypatch, add):
    from app import chunking, database
    monkeypatch.setattr(chunking, "chunk_plain", lambda text: ["c1", "c2"])
    monkeypatch.setattr(database, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(database, "add_document", add)
    monkeypatch.setattr(database, "delete_documents", lambda ids, dept: None)


def _boom(*args, **kwargs):
    raise RuntimeError(WORDS)


def _served(client, admin_headers, job_id) -> tuple[dict, str]:
    r = client.get("/api/admin/jobs?limit=200", headers=admin_headers)
    assert r.status_code == 200, r.text
    return next(j for j in r.json()["jobs"] if j["job_id"] == job_id), r.text


def test_a_failed_ingest_serves_the_fixed_sentence(monkeypatch, client, admin_headers):
    """FIX: the row and the route carry the fixed sentence; the log the words."""
    _index(monkeypatch, _boom)
    seen = _record(monkeypatch)
    job_id = jobs.create_job(source="failing.md", department="general")
    jobs._run_ingest(job_id, "failing.md", "text", "general", None)

    row, text = _served(client, admin_headers, job_id)
    assert row["status"] == "failed"
    assert PATH not in text and "malformed" not in text and "0x7f3a2c" not in text, row
    assert row["error"] == FAILED
    [(_, fields)] = [e for e in seen if e[0] == "ingest_async_error"]
    assert fields["error"] == f"RuntimeError: {WORDS}"[:300], len(fields["error"])


def test_a_crashed_worker_serves_the_fixed_sentence(monkeypatch, client, admin_headers):
    """FIX: an exception that escapes _run_ingest's own handlers fails the row
    in the worker's last handler - with its fixed sentence, not the words."""
    seen = _record(monkeypatch)
    job_id = jobs.create_job(source="crashing.md", department="general")
    monkeypatch.setattr(jobs, "_run_ingest", _boom)
    jobs.dispatch_ingest(job_id, "crashing.md", "text", "general")
    jobs._pool().submit(lambda: None).result(timeout=10)   # serial pool: drains

    row, text = _served(client, admin_headers, job_id)
    assert row["status"] == "failed"
    assert PATH not in text and "malformed" not in text and "0x7f3a2c" not in text, row
    assert row["error"] == CRASHED
    [(_, fields)] = [e for e in seen if e[0] == "ingest_worker_crashed"]
    assert fields["error"] == f"RuntimeError: {WORDS}"[:300], len(fields["error"])
    assert jobs.pending_count() == 0


def test_a_job_that_succeeds_stores_no_error(monkeypatch, client, admin_headers):
    """CONTROL: nothing failed, nothing is said."""
    _index(monkeypatch, lambda *a, **k: None)
    seen = _record(monkeypatch)
    job_id = jobs.create_job(source="fine.md", department="general")
    jobs._run_ingest(job_id, "fine.md", "text", "general", None)

    row, _ = _served(client, admin_headers, job_id)
    assert row["status"] == "complete"
    assert row["error"] is None
    assert not [e for e in seen if e[0] == "ingest_async_error"]


def test_a_quarantine_hold_keeps_its_own_sentence(monkeypatch, client, admin_headers):
    """CONTROL: the backstop's hold already says what happened, with the id
    the reviewer looks for; it is unchanged."""
    from app import quarantine
    from app.corpus_scan import QuarantinedContent

    def _add(doc_id, chunk, meta, **k):
        if chunk == "c2":
            raise QuarantinedContent("held.md", "general", "untrusted", "text",
                                     [{"type": "probe"}])

    _index(monkeypatch, _add)
    monkeypatch.setattr(quarantine, "write_quarantine_row",
                        lambda *a, **k: {"quarantine_id": "q-91"})
    job_id = jobs.create_job(source="held.md", department="general")
    jobs._run_ingest(job_id, "held.md", "text", "general", None)

    row, _ = _served(client, admin_headers, job_id)
    assert row["status"] == "failed"
    assert row["error"] == "quarantined for review (id q-91)"
