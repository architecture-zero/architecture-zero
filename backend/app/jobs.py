"""Background ingest jobs: hand a large upload to a worker thread and answer
the request now, instead of holding the connection open through chunking and
embedding.

OFF BY DEFAULT. With ENABLE_ASYNC_JOBS unset every upload ingests inside the
request, exactly as before; nothing here runs and no thread is started.

WHY A THREAD AND NOT A TASK QUEUE. The obvious shape for this is a broker and
a worker container, and it is the wrong shape for this platform: the vector
store is EMBEDDED, not a server. database.py builds a chromadb.PersistentClient
over a directory, so a worker in a second process is a second writer against
one HNSW index with no cross-process locking - the same vector-loss class that
chroma_maintenance.py exists to repair and that the compose stop_grace_period
exists to avoid. The lexical half fails the same way for a quieter reason:
_LEX_INDEX is an ordinary in-process dict, so a second process invalidates its
own copy and leaves the API serving a stale BM25 index after every queued
ingest - hybrid retrieval degraded with nothing to say so.

One process keeps both problems from existing. It also matches how this repo
already runs long work: an eval run is a background thread with a status
endpoint, which is this feature's shape exactly.

The trade is honest and it is the roadmap's next rung: this scales to one box.
Spreading ingestion across machines needs the vector store to become a server
first, and until it is, a broker would buy distribution by giving up index
integrity.

SERIAL BY CONSTRUCTION. One worker thread, not a pool. Parallel embedding would
put concurrent writers back on the same index inside the process, which is the
problem this module is avoiding, and the win here is getting the work off the
request path rather than doing more of it at once.

WHAT IS DEFERRED AND WHAT IS NOT. The endpoint keeps everything that decides
whether content may be indexed at all: extraction, the full-text injection scan,
the quarantine decision, PII redaction, and the caller's trust tier. Only
chunking, embedding and the index diff move here. A caller still learns
synchronously that their upload was withheld, and the trust tier is computed
from the live request user and carried in - never re-derived here, where there
is no user to derive it from.

BOUNDED, BECAUSE THE ENDPOINT IS. The upload handler enforces MAX_UPLOAD_MB
while reading the body specifically so one request cannot exhaust memory.
Queueing holds each pending document's extracted text until its turn, so an
unbounded queue would hand that bound straight back: N accepted uploads resident
at once. ASYNC_JOB_MAX_QUEUED caps the depth and dispatch refuses past it.

WHAT A RESTART COSTS. A queued or running job lives in this process, so a
restart loses it - the row would otherwise sit at 'running' forever, describing
work nothing is doing. reconcile_orphaned_jobs() fails those rows at boot and
says why. The document is not lost: re-upload re-queues it, and content-addressed
ids mean the chunks that did land are skipped rather than re-embedded.
"""
import hashlib
import os
import threading
import uuid
from datetime import datetime, timezone

ENABLE_ASYNC_JOBS = os.getenv("ENABLE_ASYNC_JOBS", "false").lower() == "true"
# Pending documents held in memory at once, each up to the upload handler's
# MAX_UPLOAD_MB of extracted text. Past this, dispatch refuses and the caller
# ingests synchronously or retries - a queue that accepts everything is just a
# slower way to run out of memory.
ASYNC_JOB_MAX_QUEUED = int(os.getenv("ASYNC_JOB_MAX_QUEUED", "20"))

_POOL = None
_POOL_GUARD = threading.Lock()
# Its own lock, not the pool's: the worker thread decrements this in a finally
# while the dispatching thread may be inside _pool() building the executor, and
# one lock covering both makes that ordering something to reason about rather
# than something that cannot happen.
_PENDING = 0
_PENDING_GUARD = threading.Lock()


class JobQueueFull(Exception):
    """Dispatch refused: ASYNC_JOB_MAX_QUEUED documents are already waiting."""


def async_enabled() -> bool:
    """Whether an upload should be queued.

    Read this rather than the constant, and read it as an attribute
    (`jobs.async_enabled()`), never `from app.jobs import ENABLE_ASYNC_JOBS`:
    a from-import binds the value once at import time, which makes the flag
    unpatchable in tests and turns any future runtime toggle into a fossil the
    caller can never see change.
    """
    return ENABLE_ASYNC_JOBS


def _pool():
    """Lazily built, so importing this module starts no thread on an instance
    that never enables async ingest."""
    global _POOL
    with _POOL_GUARD:
        if _POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")
        return _POOL


# -- Job rows ---------------------------------------------------------------

def create_job(source: str, department: str) -> str:
    from app.db import get_session
    from app.models import IngestJob
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_session() as s:
        s.add(IngestJob(
            job_id=job_id,
            status="queued",
            source=source,
            department=department,
            created_at=now,
        ))
        s.commit()
    return job_id


def update_job(
    job_id: str,
    *,
    status: str,
    chunks_processed: int = 0,
    chunks_total: int | None = None,
    error: str | None = None,
) -> None:
    from app.db import get_session
    from app.models import IngestJob
    from sqlalchemy import update as _update
    vals: dict = {"status": status, "chunks_processed": chunks_processed}
    if chunks_total is not None:
        vals["chunks_total"] = chunks_total
    if error is not None:
        vals["error"] = error
    if status in ("complete", "failed"):
        vals["completed_at"] = datetime.now(timezone.utc).isoformat()
    with get_session() as s:
        s.execute(_update(IngestJob).where(IngestJob.job_id == job_id).values(**vals))
        s.commit()


def list_jobs(limit: int = 100) -> list[dict]:
    from app.db import get_session
    from app.models import IngestJob
    from sqlalchemy import select
    # The dicts are built INSIDE the session on purpose: get_session commits on
    # exit and the sessionmaker expires attributes on commit, so reading these
    # rows after the with-block raises DetachedInstanceError the moment a single
    # job exists. A version of this that reads after the block passes every test
    # written against an empty table.
    with get_session() as s:
        rows = s.execute(
            select(IngestJob).order_by(IngestJob.created_at.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "job_id": r.job_id,
                "status": r.status,
                "source": r.source,
                "department": r.department,
                "chunks_processed": r.chunks_processed,
                "chunks_total": r.chunks_total,
                "error": r.error,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in rows
        ]


def reconcile_orphaned_jobs() -> dict:
    """Fail every job left queued or running by a previous process, at boot.

    Nothing is resuming them: the thread that owned them died with the process.
    Left alone the rows describe work in flight forever, which is worse than a
    failure - a status surface that lies is the thing this platform treats as
    worse than no status surface at all. Runs whether or not async jobs are
    enabled now, because the rows may predate the flag being turned off.
    """
    from app.db import get_session
    from app.models import IngestJob
    from sqlalchemy import update as _update
    now = datetime.now(timezone.utc).isoformat()
    with get_session() as s:
        res = s.execute(
            _update(IngestJob)
            .where(IngestJob.status.in_(("queued", "running")))
            .values(status="failed", completed_at=now,
                    error="interrupted by a restart - re-upload to retry")
        )
        s.commit()
        return {"orphaned_jobs_failed": res.rowcount or 0}


# -- The work ---------------------------------------------------------------

def _run_ingest(job_id: str, filename: str, text: str, department: str,
                extra_meta: dict | None = None) -> None:
    """Chunk, embed and diff one document against what is already indexed.

    extra_meta carries the endpoint's trust tier and its injection / PII tags
    onto every chunk. The quarantine DECISION was already made synchronously,
    on the full text, before this was queued.

    Imports are function-local so the module imports clean on an instance that
    never enables this, and so a patched app.database is read at call time.
    """
    from app import chunking, corpus_scan
    from app.database import add_document, delete_documents, get_source_ids
    from app.logger import log, log_error
    from app.metrics import increment

    update_job(job_id, status="running")
    # Bound BEFORE the try because both handlers below read it: widening the
    # try to cover chunking means an early failure can now reach `except` with
    # nothing assigned, and a NameError raised inside an except clause escapes
    # the handler that was supposed to record the failure - trading a stuck row
    # for a silent one.
    new_items: list[tuple[str, int, str]] = []
    # EVERYTHING from here is inside the try. It used to start below, after
    # chunking and the two index reads - so a failure in chunk_plain or
    # get_source_ids (a corrupt index, a chroma read error) escaped with the row
    # still saying "running", and nothing ever moved it. The row then described
    # work no thread was doing until the next restart, when
    # reconcile_orphaned_jobs finally failed it - which is the status-surface-
    # that-lies class this module's own docstring says is worse than no status.
    try:
        chunks = chunking.chunk_plain(text)
        update_job(job_id, status="running", chunks_total=len(chunks))

    # ADD-FIRST-PRUNE-LAST with content-addressed ids - the same set diff the
    # upload handler runs, including .setdefault (first chunk wins on duplicate
    # text) and usedforsecurity=False. It has to hash the identical string, or
    # a document that switches between the sync and queued paths matches
    # nothing on the other side and re-embeds its whole generation.
        desired: dict[str, tuple[int, str]] = {}
        for i, chunk in enumerate(chunks):
            doc_id = hashlib.md5(f"{department}::{filename}::{chunk}".encode(),
                                 usedforsecurity=False).hexdigest()
            desired.setdefault(doc_id, (i, chunk))
        existing = set(get_source_ids(filename, department))
        new_items = [(doc_id, i, chunk) for doc_id, (i, chunk) in desired.items()
                     if doc_id not in existing]
        done_base = len(chunks) - len(new_items)   # already indexed verbatim

        for n, (doc_id, i, chunk) in enumerate(new_items):
            add_document(doc_id, chunk,
                         {"source": filename, "chunk": i, **(extra_meta or {})},
                         department=department)
            # Progress every 10 chunks rather than every chunk: this is a
            # status readout, not an audit log, and each update is a write.
            if (n + 1) % 10 == 0 or n == len(new_items) - 1:
                update_job(job_id, status="running",
                           chunks_processed=done_base + n + 1,
                           chunks_total=len(chunks))
        # Only once every chunk of the new version is indexed do the chunks it
        # no longer contains get dropped. A failure above leaves the previous
        # generation whole.
        stale = sorted(existing - desired.keys())
        if stale:
            delete_documents(stale, department)
        increment("ingest_total")
        log("ingest_async_complete", job_id=job_id, source=filename,
            chunks=len(chunks), department=department)
        update_job(job_id, status="complete", chunks_processed=len(chunks),
                   chunks_total=len(chunks))
    except corpus_scan.QuarantinedContent as q:
        # Backstop for a pattern that anchors at a chunk boundary and so fires
        # per-chunk even though the full text passed the scan before dispatch.
        # Drop what THIS job added and quarantine the whole document; the
        # previous generation stays indexed.
        #
        # This is the upload handler's backstop, not a whole-source unwind. For
        # an uploaded document the indexed chunk text is the only copy that
        # exists - there is no file on disk to re-ingest from - so unwinding the
        # source would destroy an earlier version that passed its own scan,
        # over a boundary artifact in a new one. The new text is recoverable
        # from the quarantine row; the old version would not be recoverable
        # from anywhere.
        from app.quarantine import write_quarantine_row
        added = [doc_id for doc_id, _, _ in new_items]
        if added:
            delete_documents(added, department)
        res = write_quarantine_row(filename, department, q.trust_tier, text,
                                   q.findings)
        update_job(job_id, status="failed",
                   error=f"quarantined for review (id {res['quarantine_id']})")
    except Exception as e:
        log_error("ingest_async_error", job_id=job_id, source=filename,
                  error=str(e))
        update_job(job_id, status="failed", error=str(e))


def dispatch_ingest(job_id: str, filename: str, text: str, department: str,
                    extra_meta: dict | None = None) -> None:
    """Queue one document. Raises JobQueueFull when the depth cap is reached.

    The caller creates the job row first so a refusal here can mark it failed
    rather than leave a row queued behind a queue that never accepted it.
    """
    global _PENDING
    with _PENDING_GUARD:
        if _PENDING >= ASYNC_JOB_MAX_QUEUED:
            raise JobQueueFull(
                f"{_PENDING} documents already queued (ASYNC_JOB_MAX_QUEUED="
                f"{ASYNC_JOB_MAX_QUEUED})")
        _PENDING += 1

    def _release():
        global _PENDING
        with _PENDING_GUARD:
            _PENDING -= 1

    def _work():
        try:
            _run_ingest(job_id, filename, text, department, extra_meta)
        finally:
            _release()

    try:
        _pool().submit(_work)
    except Exception:
        # The slot was reserved above; a submit that never ran has to give it
        # back, or a failing executor walks the count up to the cap and wedges
        # dispatch permanently against a queue holding nothing.
        _release()
        raise


def pending_count() -> int:
    """Documents queued or in flight right now - the depth the cap bounds."""
    return _PENDING
