"""The queued ingest path: the same write shape as the synchronous one, plus
the bounds that keep an accepted queue from undoing the endpoint's own limits.

Three things here are pins on decisions rather than on behavior, and each one
covers a break that is invisible without it:

- THE ID SCHEME IS PINNED ACROSS BOTH PATHS BEHAVIORALLY, not by comparing
  source text. The queued worker and the upload handler each build content
  addressed ids by hashing a string assembled inline; if the two ever hash
  differently, nothing fails - the second path simply matches no existing chunk
  and re-embeds the document's whole generation as a full swap. Correct output,
  silently wrong work, and no test would have said so.

- THE QUARANTINE BACKSTOP DROPS ONLY THIS JOB'S CHUNKS. A whole-source unwind
  is the other reasonable reading and it is the wrong one here: for an uploaded
  document the indexed chunk text is the only copy in existence, so unwinding
  would destroy a previous version that passed its own scan because a new
  version tripped a pattern at a chunk boundary. Asserted two-sided - the old
  ids must still be there.

- THE FLAG MAY NOT BE FROM-IMPORTED, checked structurally. Bound once at import
  time it is unpatchable and can never see a change; the same class the startup
  ingest flag is pinned against.

The worker runs in-process, so these call _run_ingest directly rather than
racing a thread: the queue mechanics get their own tests below.
"""
import ast
import hashlib
import pathlib

import pytest

from app import jobs

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
TESTS = pathlib.Path(__file__).resolve().parent


def _cid(dept, name, chunk):
    return hashlib.md5(f"{dept}::{name}::{chunk}".encode(),
                       usedforsecurity=False).hexdigest()


@pytest.fixture(autouse=True)
def _reset_queue_depth():
    """The pending counter is a module global by design (it bounds the whole
    process, not one caller), so a test that leaves it raised would starve the
    next one's dispatch."""
    jobs._PENDING = 0
    yield
    jobs._PENDING = 0


# -- The write shape ---------------------------------------------------------

def test_add_first_prune_last_with_content_ids(monkeypatch):
    from app import chunking, database
    order = []
    chunks = ["chunk one", "chunk two"]
    old_only_id = _cid("general", "doc.md", "chunk gone")
    kept_id = _cid("general", "doc.md", "chunk one")
    monkeypatch.setattr(chunking, "chunk_plain", lambda text: list(chunks))
    monkeypatch.setattr(database, "get_source_ids",
                        lambda name, dept: [kept_id, old_only_id])
    monkeypatch.setattr(database, "add_document",
                        lambda doc_id, *a, **k: order.append(("add", doc_id)))
    monkeypatch.setattr(database, "delete_documents",
                        lambda ids, dept: order.append(("prune", list(ids))))
    job_id = jobs.create_job(source="doc.md", department="general")
    jobs._run_ingest(job_id, "doc.md", "chunking is patched", "general",
                     {"trust": "curated"})
    # Only the NEW chunk embeds - kept_id is already indexed verbatim - and the
    # stale id is pruned strictly last.
    assert order == [("add", _cid("general", "doc.md", "chunk two")),
                     ("prune", [old_only_id])]
    row = next(j for j in jobs.list_jobs() if j["job_id"] == job_id)
    assert row["status"] == "complete"


def test_failed_add_leaves_previous_generation_intact(monkeypatch):
    from app import chunking, database
    pruned = {"called": False}
    monkeypatch.setattr(chunking, "chunk_plain", lambda text: ["c1", "c2"])
    monkeypatch.setattr(database, "get_source_ids",
                        lambda name, dept: [_cid("general", "doc.md", "old")])
    monkeypatch.setattr(database, "add_document",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("embed down")))
    monkeypatch.setattr(database, "delete_documents",
                        lambda ids, dept: pruned.update(called=True))
    job_id = jobs.create_job(source="doc.md", department="general")
    jobs._run_ingest(job_id, "doc.md", "text", "general", None)
    assert not pruned["called"], "stale prune ran despite a failed add"
    row = next(j for j in jobs.list_jobs() if j["job_id"] == job_id)
    assert row["status"] == "failed"
    assert "embed down" in (row["error"] or "")


def test_the_extra_meta_reaches_every_chunk(monkeypatch):
    """The trust tier and the scan tags are computed at the endpoint, from the
    request user, and carried in. A worker that dropped them would index
    third-party content with no provenance tier at all."""
    from app import chunking, database
    seen = []
    monkeypatch.setattr(chunking, "chunk_plain", lambda text: ["a", "b"])
    monkeypatch.setattr(database, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(database, "add_document",
                        lambda doc_id, chunk, meta, **k: seen.append(meta))
    job_id = jobs.create_job(source="doc.md", department="general")
    jobs._run_ingest(job_id, "doc.md", "text", "general",
                     {"trust": "untrusted", "injection_flagged": "true"})
    assert len(seen) == 2
    for meta in seen:
        assert meta["trust"] == "untrusted"
        assert meta["injection_flagged"] == "true"
        assert meta["source"] == "doc.md"


def test_list_jobs_survives_real_rows():
    """get_session commits on exit and the sessionmaker expires attributes on
    commit, so dicts built after the with-block raise DetachedInstanceError the
    moment one row exists. A version with that bug passes every assertion
    written against an empty table."""
    job_id = jobs.create_job(source="rowcheck.md", department="general")
    assert any(j["job_id"] == job_id for j in jobs.list_jobs())


# -- The quarantine backstop, and what it must NOT destroy -------------------

def test_quarantine_backstop_drops_only_this_jobs_chunks(monkeypatch):
    """A chunk-boundary pattern can fire per-chunk after the full text passed
    the endpoint's scan. The unwind covers what THIS job added and stops there.

    Two-sided on the part that matters: delete_source must never be called and
    the previous generation's id must survive. The new text is recoverable from
    the quarantine row; the old version is recoverable from nowhere, because an
    uploaded document has no file on disk behind it.
    """
    from app import chunking, database, quarantine
    from app.corpus_scan import QuarantinedContent
    deleted, unwound, rows = [], [], []
    previous_id = _cid("general", "doc.md", "the version already indexed")
    monkeypatch.setattr(chunking, "chunk_plain", lambda text: ["c1", "c2"])
    monkeypatch.setattr(database, "get_source_ids", lambda name, dept: [previous_id])

    def _add(doc_id, chunk, meta, **k):
        if chunk == "c2":
            raise QuarantinedContent("doc.md", "general", "untrusted", "text",
                                     [{"type": "probe"}])

    monkeypatch.setattr(database, "add_document", _add)
    monkeypatch.setattr(database, "delete_documents",
                        lambda ids, dept: deleted.extend(ids))
    monkeypatch.setattr(database, "delete_source",
                        lambda name, dept: unwound.append(name))
    monkeypatch.setattr(quarantine, "write_quarantine_row",
                        lambda *a, **k: (rows.append(a), {"quarantine_id": "q-77"})[1])

    job_id = jobs.create_job(source="doc.md", department="general")
    jobs._run_ingest(job_id, "doc.md", "text", "general", None)

    assert not unwound, "the whole source was unwound - the previous version is gone"
    assert previous_id not in deleted, "the already-indexed generation was deleted"
    assert set(deleted) == {_cid("general", "doc.md", "c1"),
                            _cid("general", "doc.md", "c2")}
    assert rows, "no quarantine row written"
    row = next(j for j in jobs.list_jobs() if j["job_id"] == job_id)
    assert row["status"] == "failed"
    assert "q-77" in (row["error"] or "")


# -- The two paths must agree on the id -------------------------------------

def test_queued_and_sync_paths_produce_identical_chunk_ids(client, admin_headers,
                                                           monkeypatch):
    """The break this covers is silent: different hashing means the queued path
    matches nothing already indexed and re-embeds the whole document, with the
    right chunks in the index either way. Compared by RUNNING both, not by
    matching source text - the two build the string from differently named
    locals, so a textual pin would compare nothing."""
    from app import database
    from app.routers import kb
    text = "Onboarding notes. Nothing here trips a scanner."
    sync_ids, async_ids = [], []

    monkeypatch.setattr(database, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(kb, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(kb, "add_document",
                        lambda doc_id, *a, **k: sync_ids.append(doc_id))
    r = client.post("/api/ingest/upload",
                    files={"file": ("notes.md", text.encode(), "text/plain")},
                    data={"department": "general"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ingested"

    monkeypatch.setattr(database, "add_document",
                        lambda doc_id, *a, **k: async_ids.append(doc_id))
    job_id = jobs.create_job(source="notes.md", department="general")
    jobs._run_ingest(job_id, "notes.md", text, "general", {"trust": "curated"})

    assert sync_ids, "the synchronous path indexed nothing - the comparison is empty"
    assert sync_ids == async_ids


# -- The queue bounds --------------------------------------------------------

def test_dispatch_refuses_past_the_depth_cap(monkeypatch):
    """Each pending document holds its extracted text in memory. The upload
    handler caps ONE body at MAX_UPLOAD_MB; without this cap, queueing hands
    that bound back and N accepted uploads sit resident at once."""
    monkeypatch.setattr(jobs, "ASYNC_JOB_MAX_QUEUED", 2)
    started = []
    monkeypatch.setattr(jobs, "_run_ingest",
                        lambda *a, **k: started.append(a[0]))
    jobs._PENDING = 2
    with pytest.raises(jobs.JobQueueFull):
        jobs.dispatch_ingest("j1", "doc.md", "text", "general")
    assert not started


def test_a_refused_dispatch_releases_its_slot(monkeypatch):
    """The slot is reserved before submit. A submit that never runs has to give
    it back, or a failing executor walks the count to the cap and wedges
    dispatch against a queue holding nothing."""
    monkeypatch.setattr(jobs, "ASYNC_JOB_MAX_QUEUED", 4)

    class _DeadPool:
        def submit(self, fn):
            raise RuntimeError("executor is shut down")

    monkeypatch.setattr(jobs, "_pool", lambda: _DeadPool())
    before = jobs.pending_count()
    with pytest.raises(RuntimeError):
        jobs.dispatch_ingest("j1", "doc.md", "text", "general")
    assert jobs.pending_count() == before


def test_a_dispatched_job_runs_and_releases(monkeypatch):
    """The pool leg itself, once: submit reaches _run_ingest and the slot comes
    back afterwards. Everything above calls _run_ingest directly, so without
    this nothing covers the thread hand-off at all."""
    done = []
    monkeypatch.setattr(jobs, "_run_ingest",
                        lambda job_id, *a, **k: done.append(job_id))
    jobs.dispatch_ingest("j-thread", "doc.md", "text", "general")
    jobs._pool().submit(lambda: None).result(timeout=10)   # serial pool: drains
    assert done == ["j-thread"]
    assert jobs.pending_count() == 0


# -- What a restart costs ----------------------------------------------------

def test_reconcile_fails_rows_a_restart_orphaned():
    """Queued and running rows live in the process that owns the thread. After
    a restart nothing is resuming them, so a row still claiming progress is a
    status surface telling the operator something untrue."""
    queued = jobs.create_job(source="orphan-queued.md", department="general")
    running = jobs.create_job(source="orphan-running.md", department="general")
    jobs.update_job(running, status="running", chunks_total=9)
    done = jobs.create_job(source="already-done.md", department="general")
    jobs.update_job(done, status="complete", chunks_processed=3, chunks_total=3)

    res = jobs.reconcile_orphaned_jobs()
    assert res["orphaned_jobs_failed"] >= 2

    rows = {j["job_id"]: j for j in jobs.list_jobs(limit=200)}
    assert rows[queued]["status"] == "failed"
    assert rows[running]["status"] == "failed"
    assert "restart" in (rows[running]["error"] or "")
    assert rows[running]["completed_at"]
    # A finished job is not rewritten - the sweep touches the in-flight states
    # only, or every boot would relabel history.
    assert rows[done]["status"] == "complete"
    assert rows[done]["error"] is None


# -- The endpoint ------------------------------------------------------------

def test_upload_queues_when_the_flag_is_on(client, admin_headers, monkeypatch):
    from app.routers import kb
    dispatched = {}
    monkeypatch.setattr(jobs, "ENABLE_ASYNC_JOBS", True)
    monkeypatch.setattr(jobs, "dispatch_ingest",
                        lambda job_id, name, text, dept, extra_meta=None:
                        dispatched.update(job_id=job_id, name=name, text=text,
                                          dept=dept, meta=extra_meta))
    monkeypatch.setattr(kb, "add_document",
                        lambda *a, **k: pytest.fail("queued upload embedded inline"))

    r = client.post("/api/ingest/upload",
                    files={"file": ("big.md", b"Queued document body.", "text/plain")},
                    data={"department": "general"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"] == dispatched["job_id"]
    # Same key set as the synchronous return, so one client reads both.
    assert set(body) == {"status", "job_id", "source", "chunks", "department", "pii"}
    assert body["chunks"] is None
    # The trust tier is decided at the endpoint and travels with the job - the
    # worker has no request user to derive it from.
    assert dispatched["meta"]["trust"]
    assert dispatched["name"] == "big.md"


def test_a_full_queue_answers_503_and_closes_its_job_row(client, admin_headers,
                                                         monkeypatch):
    """The job row is created before dispatch, so a refusal has to close it.
    Left queued it would describe a document no queue ever accepted - and the
    next boot's reconcile would report it as interrupted work."""
    monkeypatch.setattr(jobs, "ENABLE_ASYNC_JOBS", True)

    def _full(*a, **k):
        raise jobs.JobQueueFull("20 documents already queued")

    monkeypatch.setattr(jobs, "dispatch_ingest", _full)
    r = client.post("/api/ingest/upload",
                    files={"file": ("late.md", b"Body.", "text/plain")},
                    data={"department": "general"}, headers=admin_headers)
    assert r.status_code == 503, r.text
    assert "queue is full" in r.json()["detail"].lower()
    row = next(j for j in jobs.list_jobs(limit=200) if j["source"] == "late.md")
    assert row["status"] == "failed"
    assert row["completed_at"]


def test_jobs_endpoint_reports_posture_not_just_rows(client, admin_headers):
    r = client.get("/api/admin/jobs", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # A list of finished jobs on an instance with async ingest switched OFF
    # reads as a working queue unless the payload says which it is.
    assert body["enabled"] is False
    assert body["queued"] == 0
    assert body["max_queued"] == jobs.ASYNC_JOB_MAX_QUEUED
    assert isinstance(body["jobs"], list)


def test_jobs_endpoint_bounds_its_limit(client, admin_headers):
    """An unbounded limit loads the whole table into one response."""
    assert client.get("/api/admin/jobs?limit=100000",
                      headers=admin_headers).status_code == 422
    assert client.get("/api/admin/jobs?limit=0",
                      headers=admin_headers).status_code == 422


def test_jobs_endpoint_requires_manage_kb(client, admin_headers):
    """An Owner token proves nothing here - require_permission bypasses for
    owners, so the scope has to be exercised by a role that lacks it. Member
    has chat and view_history and no manage_kb."""
    created = client.post("/api/users",
                          json={"username": "jobsmember", "password": "MemberPass1",
                                "role": "member"}, headers=admin_headers)
    assert created.status_code in (200, 201, 409), created.text
    login = client.post("/api/auth/login",
                        json={"username": "jobsmember", "password": "MemberPass1"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/api/admin/jobs", headers=headers).status_code == 403
    assert client.get("/api/admin/jobs").status_code == 401


# -- Structural ---------------------------------------------------------------

def test_the_async_flag_is_never_from_imported():
    """A from-import binds the value once at import time: the reader can never
    see a change, and a test that patches app.jobs patches something nothing
    reads. Same class the startup ingest flag is pinned against, and the reason
    the callers go through jobs.async_enabled() instead.
    """
    offenders = []
    files = [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]
    files += sorted(TESTS.glob("test_*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") in ("app.jobs", "jobs"):
                for alias in node.names:
                    if alias.name == "ENABLE_ASYNC_JOBS":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "ENABLE_ASYNC_JOBS must be read as an attribute (jobs.async_enabled() or "
        f"jobs.ENABLE_ASYNC_JOBS), never from-imported: {offenders}")
