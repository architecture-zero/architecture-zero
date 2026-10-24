"""Every writer of a source takes that source's lock - not only the file syncs.

A source delete landing mid-upload of the same source took the chunks written
so far and left the rest of the file indexed; a quarantine release could
re-ingest an old snapshot beside an upload of the same file; an upload and a
file sync of one name diffed against each other. The upload keeps its global
lock and takes the source's lock inside it, in the fixed order written beside
ingest_sync._source_lock.

Each test pauses an upload inside its first chunk write, sends the second
writer in, and gives it half a second to land where an unlocked writer would.
"""
import inspect
import threading

from fastapi import HTTPException

from app import ingest_sync, jobs
from app.routers import kb

OWNER = {"id": 1, "username": "owner", "role": "owner"}
BODY = "\n".join(f"Policy line {i}: invoices are due within thirty days."
                 for i in range(80)).encode()


def _held(source):
    """Whether this source's lock is held right now (no registry reads as no)."""
    lock = getattr(ingest_sync, "_ingest_locks", {}).get(source)
    return bool(lock and lock.locked())


class _Index:
    """A stand-in vector store: chunk id -> source."""

    def __init__(self):
        self._guard = threading.Lock()
        self.rows = {}

    def ids(self, source, department=None):
        with self._guard:
            return [i for i, s in self.rows.items() if s == source]

    def put(self, doc_id, source):
        with self._guard:
            self.rows[doc_id] = source

    def drop(self, ids, department=None):
        with self._guard:
            for i in ids:
                self.rows.pop(i, None)

    def drop_source(self, source, department=None):
        self.drop(self.ids(source))


def _paused_upload_index(monkeypatch, on_add=lambda exempt: None):
    """The router's index seams over one _Index. An upload's first chunk write
    pauses - mid is set, then it waits for go - so a second writer can arrive
    in the middle of it. A release writes quarantine_exempt and never pauses."""
    index, mid, go = _Index(), threading.Event(), threading.Event()

    def add_document(doc_id, text, meta, department=None, quarantine_exempt=False):
        index.put(doc_id, meta["source"])
        on_add(quarantine_exempt)
        if not quarantine_exempt and not mid.is_set():
            mid.set()
            go.wait(timeout=5)

    monkeypatch.setattr(kb, "add_document", add_document)
    monkeypatch.setattr(kb, "get_source_ids", index.ids)
    monkeypatch.setattr(kb, "delete_documents", index.drop)
    monkeypatch.setattr(kb, "delete_source", index.drop_source)
    return index, mid, go


def _start_upload(name):
    upload = threading.Thread(target=kb._ingest_upload,
                              args=(name, name.rsplit(".", 1)[-1], BODY, "general", OWNER))
    upload.start()
    return upload


def test_a_delete_mid_upload_leaves_no_chunk_of_the_source(monkeypatch):
    source = "lock-probe-delete.txt"
    index, mid, go = _paused_upload_index(monkeypatch)
    upload = _start_upload(source)
    assert mid.wait(timeout=5)
    delete = threading.Thread(target=kb.remove_source,
                              kwargs={"source": source, "current_user": OWNER})
    delete.start()
    delete.join(timeout=0.5)     # an unlocked delete lands now, mid-upload
    go.set()
    for t in (upload, delete):
        t.join(timeout=10)
    assert index.ids(source) == [], "a delete mid-upload left the upload's later chunks indexed"


def test_a_release_cannot_interleave_with_an_upload(monkeypatch):
    """The release waits for the upload, re-checks its row once it holds the
    lock, and finds it superseded by that upload - so it writes nothing."""
    from app.db import get_session
    from app.models import QuarantinedDoc
    from app.quarantine import write_quarantine_row
    source = "lock-probe-release.txt"
    writes, outcome = [], {}
    index, mid, go = _paused_upload_index(
        monkeypatch, on_add=lambda exempt: writes.append("release" if exempt else "upload"))
    held = write_quarantine_row(source, "general", "untrusted", "The old policy text.",
                                [{"type": "instruction_override", "severity": "high"}])["quarantine_id"]

    def release():
        try:
            outcome["answer"] = kb.admin_release_quarantine(held, current_user=OWNER)
        except HTTPException as e:
            outcome["status"] = e.status_code

    try:
        upload = _start_upload(source)
        assert mid.wait(timeout=5)
        releasing = threading.Thread(target=release)
        releasing.start()
        releasing.join(timeout=0.5)     # an unlocked release re-ingests now, mid-upload
        go.set()
        for t in (upload, releasing):
            t.join(timeout=10)
        assert "release" not in writes, f"the release wrote beside the upload: {writes}"
        assert outcome == {"status": 404}
        with get_session() as db:
            assert db.get(QuarantinedDoc, held).status == "superseded"
    finally:
        with get_session() as db:
            row = db.get(QuarantinedDoc, held)
            if row:
                db.delete(row)


def test_an_upload_and_a_sync_of_one_source_name_take_turns(monkeypatch):
    source = "lock-probe-faq.md"
    order = []
    index, mid, go = _paused_upload_index(monkeypatch, on_add=lambda exempt: order.append("upload"))
    monkeypatch.setattr(ingest_sync, "get_source_ids", lambda name, dept=None: [])
    monkeypatch.setattr(ingest_sync, "add_documents_batch",
                        lambda entries, department=None, quarantine_exempt=False:
                        order.append("sync") or len(entries))
    monkeypatch.setattr(ingest_sync, "delete_documents", lambda ids, dept=None: None)
    monkeypatch.setattr(ingest_sync, "delete_source", lambda name, dept=None: None)
    monkeypatch.setattr(ingest_sync, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_sync, "_save_ingest_state", lambda state: None)
    upload = _start_upload(source)
    assert mid.wait(timeout=5)
    sync = threading.Thread(target=ingest_sync._ingest_file, args=(source, "## FAQ\n\nAnswers.\n"))
    sync.start()
    sync.join(timeout=0.5)       # an unlocked sync writes now, mid-upload
    go.set()
    for t in (upload, sync):
        t.join(timeout=10)
    assert order[-1] == "sync" and "sync" not in order[:-1], f"the sync wrote inside the upload: {order}"


def test_the_queued_worker_writes_under_the_sources_lock(monkeypatch):
    from app import chunking, database
    source, held = "lock-probe-queued.md", []
    monkeypatch.setattr(chunking, "chunk_plain", lambda text: ["c1", "c2"])
    monkeypatch.setattr(database, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(database, "add_document",
                        lambda doc_id, text, meta, department=None: held.append(_held(source)))
    monkeypatch.setattr(database, "delete_documents", lambda ids, dept=None: None)
    job_id = jobs.create_job(source=source, department="general")
    jobs._run_ingest(job_id, source, "text", "general", None)
    assert held == [True, True]


def test_no_route_takes_a_source_lock_on_the_event_loop():
    """A blocking acquire on the loop, while another writer holds the lock,
    would stall every request. The routes that take a source's lock are plain
    def routes, which FastAPI runs on its threadpool; the upload is async only
    for its body read and hands the write to the threadpool."""
    for fn in (kb.remove_source, kb.admin_release_quarantine, kb.kb_sync,
               kb.prune_orphans_endpoint, kb._ingest_upload):
        assert not inspect.iscoroutinefunction(fn), fn.__name__
    assert "run_in_threadpool(_ingest_upload" in inspect.getsource(kb.upload_file)
