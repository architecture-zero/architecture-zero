"""A failed quarantine supersede logs a bounded error (2026-10-30).

Once a new version of a file is indexed, an earlier upload of it still held
for review leaves the queue as superseded. A failure there is bookkeeping -
the upload stands - so it is logged as quarantine_supersede_failed, from the
upload handler and from the queued path alike. Both logged str(e) whole; the
rule for an internal error's detail is 300 characters.

FIX: on either path, a supersede that raises leaves the upload standing and
logs the exception's words cut to 300. CONTROL: a supersede that succeeds
logs no error.
"""
from app.routers import kb

WORDS = ("database is locked while resolving held rows in /app/data/app.db: "
         + "x" * 2000)


def _boom(*args, **kwargs):
    raise RuntimeError(WORDS)


def _record(monkeypatch, module) -> list:
    seen: list = []
    monkeypatch.setattr(module, "log_error",
                        lambda event, **kw: seen.append((event, kw)))
    return seen


def _upload(monkeypatch, resolve) -> dict:
    monkeypatch.setattr(kb, "_ingest_trust", lambda user, requested=None: "curated")
    monkeypatch.setattr(kb, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(kb, "add_document", lambda *a, **k: None)
    monkeypatch.setattr(kb, "resolve_moot_holds", resolve)
    return kb._ingest_upload("supersede-bound.md", "md", b"Onboarding notes.",
                             "general", {})


def test_the_upload_path_logs_a_bounded_supersede_error(monkeypatch):
    """FIX (upload handler): the upload stands, the log keeps 300."""
    seen = _record(monkeypatch, kb)
    out = _upload(monkeypatch, _boom)
    assert out["status"] == "ingested"
    [(_, fields)] = [e for e in seen if e[0] == "quarantine_supersede_failed"]
    assert fields["error"] == WORDS[:300], len(fields["error"])


def test_the_upload_path_logs_nothing_when_the_supersede_succeeds(monkeypatch):
    """CONTROL (upload handler)."""
    seen = _record(monkeypatch, kb)
    out = _upload(monkeypatch, lambda *a, **k: 0)
    assert out["status"] == "ingested"
    assert not [e for e in seen if e[0] == "quarantine_supersede_failed"]


def _queued(monkeypatch, resolve) -> tuple[str, list]:
    from app import chunking, database, jobs, logger, quarantine
    monkeypatch.setattr(chunking, "chunk_plain", lambda text: ["c1"])
    monkeypatch.setattr(database, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(database, "add_document", lambda *a, **k: None)
    monkeypatch.setattr(database, "delete_documents", lambda ids, dept: None)
    monkeypatch.setattr(quarantine, "resolve_moot_holds", resolve)
    # The queued path imports log_error from app.logger at call time.
    seen = _record(monkeypatch, logger)
    job_id = jobs.create_job(source="doc.md", department="general")
    jobs._run_ingest(job_id, "doc.md", "text", "general", None)
    status = next(j for j in jobs.list_jobs() if j["job_id"] == job_id)["status"]
    return status, seen


def test_the_queued_path_logs_a_bounded_supersede_error(monkeypatch):
    """FIX (queued path): the job completes, the log keeps 300."""
    status, seen = _queued(monkeypatch, _boom)
    assert status == "complete"
    [(_, fields)] = [e for e in seen if e[0] == "quarantine_supersede_failed"]
    assert fields["error"] == WORDS[:300], len(fields["error"])


def test_the_queued_path_logs_nothing_when_the_supersede_succeeds(monkeypatch):
    """CONTROL (queued path)."""
    status, seen = _queued(monkeypatch, lambda *a, **k: 0)
    assert status == "complete"
    assert not [e for e in seen if e[0] == "quarantine_supersede_failed"]
