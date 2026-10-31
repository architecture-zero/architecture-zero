"""The jobs route serves only the sentences the code writes (2026-10-30).

A failed queued ingest stores a fixed sentence since the same day
(test_ingest_job_fixed_error.py). A row failed BEFORE that change keeps the
words it was stored with - str(e) from the ingest's catch-all, "worker
crashed: <e>" from the worker's last handler: index file paths, object reprs
- and GET /api/admin/jobs served the column as stored, so the Ingestion Queue
tab still showed them wherever ENABLE_ASYNC_JOBS had been on. Read side, no
data rewrite: the stored row is untouched, and the route serves a stored
error only when it is one of the sentences the code writes today, a fixed
generic sentence otherwise.

FIX: an old row's raw exception words, and an old "worker crashed: <e>"
row, each serve the generic sentence and none of the words (both were served
whole before). CONTROL: every sentence the code writes today - the two fixed
failures, the restart's, a quarantine hold's with its id, a full queue's
refusal from the real dispatch - is served as stored, and a row with no
error serves none.
"""
import pytest

from app import jobs

OLD_WORDS = ("chroma upsert failed for /app/data/chroma/7f3a/data_level0.bin: "
             "database disk image is malformed <chromadb.api.models.Collection "
             "object at 0x7f3a2c>")
GENERIC = "The job failed. See the server log."


@pytest.fixture(autouse=True)
def _reset_queue_depth():
    jobs._PENDING = 0
    yield
    jobs._PENDING = 0


def _stored(error: str | None, source: str = "old.md") -> str:
    """A failed row holding `error` as written - the write the old code made."""
    job_id = jobs.create_job(source=source, department="general")
    jobs.update_job(job_id, status="failed", error=error)
    return job_id


def _served(client, admin_headers, job_id) -> tuple[dict, str]:
    r = client.get("/api/admin/jobs?limit=200", headers=admin_headers)
    assert r.status_code == 200, r.text
    return next(j for j in r.json()["jobs"] if j["job_id"] == job_id), r.text


@pytest.mark.parametrize("old", [OLD_WORDS, f"worker crashed: {OLD_WORDS}"],
                         ids=["old-catch-all-words", "old-worker-crashed-words"])
def test_an_old_rows_stored_words_are_not_served(client, admin_headers, old):
    """FIX: the generic sentence, none of the words."""
    job_id = _stored(old)
    row, text = _served(client, admin_headers, job_id)
    assert row["status"] == "failed"
    assert row["error"] == GENERIC, row["error"]
    for word in ("/app/data/chroma", "malformed", "0x7f3a2c", "worker crashed:"):
        assert word not in text, word


@pytest.mark.parametrize("written", [
    "The document could not be indexed. See the server log.",
    "The ingest worker crashed. See the server log.",
    "interrupted by a restart - re-upload to retry",
    "quarantined for review (id 91)",
], ids=["ingest-failed", "worker-crashed", "restart", "quarantine-hold"])
def test_every_sentence_the_code_writes_is_served_as_stored(client, admin_headers, written):
    """CONTROL: what the code writes today reaches the tab unchanged."""
    row, _ = _served(client, admin_headers, _stored(written))
    assert row["error"] == written


def test_a_full_queues_refusal_is_served_as_stored(monkeypatch, client, admin_headers):
    """CONTROL: the real dispatch's refusal, written by the upload route."""
    monkeypatch.setattr(jobs, "ENABLE_ASYNC_JOBS", True)
    monkeypatch.setattr(jobs, "ASYNC_JOB_MAX_QUEUED", 0)
    r = client.post("/api/ingest/upload",
                    files={"file": ("full-queue.md", b"Body.", "text/plain")},
                    data={"department": "general"}, headers=admin_headers)
    assert r.status_code == 503, r.text
    listed = client.get("/api/admin/jobs?limit=200", headers=admin_headers).json()["jobs"]
    row = next(j for j in listed if j["source"] == "full-queue.md")
    assert row["error"] == "0 documents already queued (ASYNC_JOB_MAX_QUEUED=0)"


def test_a_row_with_no_error_serves_none(client, admin_headers):
    """CONTROL: a job that has not failed says nothing."""
    job_id = jobs.create_job(source="queued.md", department="general")
    row, _ = _served(client, admin_headers, job_id)
    assert row["error"] is None
