"""Quarantine row persistence for the untrusted-corpus injection gate.

Factored out of main.py so any ingestion module - including one that cannot
import main without a cycle (a connector sync) - writes review rows through
the SAME code path as /api/ingest and /api/ingest/upload.
"""
import datetime as _dt
import json

from app.corpus_scan import finding_types
from app.db import get_session
from app.logger import log
from app.metrics import increment
from app.models import QuarantinedDoc


def write_quarantine_row(source: str, department: str | None, trust: str,
                         text: str, findings: list[dict]) -> dict:
    """Persist withheld content for owner review and answer the caller. The
    content was NEVER embedded - review happens at /api/admin/kb/quarantine."""
    with get_session() as db:
        row = QuarantinedDoc(
            source=source, department=department or "general", trust_tier=trust,
            text=text, findings=json.dumps(findings), status="held",
            created_at=_dt.datetime.utcnow().isoformat())
        db.add(row)
        db.flush()
        qid = row.id
    increment("kb_quarantined_total")
    log("injection_detected", source=source, trust=trust,
        types=finding_types(findings), quarantined=True, quarantine_id=qid)
    return {"status": "quarantined", "quarantine_id": qid, "source": source,
            "findings": findings,
            "detail": "Injection-shaped content withheld from the knowledge base; review it via the quarantine queue (GET /api/admin/kb/quarantine)."}


def resolve_moot_holds(source: str, department: str | None = None) -> int:
    """A held row is moot the moment the SAME source ingests into the corpus:
    the live version is indexed, so the held snapshot is outdated (rule tuning
    between quarantine and re-sync can otherwise re-quarantine a benign
    document on every sync, outliving its own review-delete). Marks such rows
    'superseded' (not 'deleted': the owner never reviewed them - the status
    keeps the audit honest) so the review queue, which lists held only, shows
    real decisions.

    `department` narrows the match for a caller that indexed one (source,
    department) pair - an upload: the same file name in another department is
    another document, and its hold is not moot. A caller whose source is a
    unique id may pass the source alone.

    Both upload paths call this once the new version is fully indexed (the
    synchronous handler and the queued worker). Until 2026-10-03 nothing
    called it - defined and never called, found by the module-hygiene check -
    so a held upload outlived its clean replacement, and releasing it later
    put the OLD text back over the new one (release is add-then-prune over the
    same source)."""
    from sqlalchemy import select
    now = _dt.datetime.utcnow().isoformat()
    stmt = select(QuarantinedDoc).where(QuarantinedDoc.source == source,
                                        QuarantinedDoc.status == "held")
    if department is not None:
        # write_quarantine_row stores an empty department as "general".
        stmt = stmt.where(QuarantinedDoc.department == (department or "general"))
    with get_session() as db:
        rows = db.execute(stmt).scalars().all()
        for r in rows:
            r.status = "superseded"
            r.reviewed_at = now
        n = len(rows)
    if n:
        log("quarantine_hold_superseded", source=source, count=n)
    return n
