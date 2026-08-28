"""Knowledge base: ingest, upload, sources, departments, quarantine, PII.

Eighth router out of main.py. Same rules: no prefix, full literal paths, guards
verbatim on the handlers, never `from app.main import ...`.

The ingest-sync machinery this router calls into (_sync_knowledge_dir,
_sync_docs, _prune_orphan_docs, KNOWLEDGE_DIR, _WATCHED_EXTS) now lives in
app/ingest_sync.py, which main's startup hooks drive as well. Both import it;
neither re-exports it.

MAX_UPLOAD_MB and _UPLOAD_CHUNK_BYTES move here rather than to runtime_config:
the upload handler is their only reader. tests/test_review_remediation imports
both from this module now - if a copy were left behind in main, that test would
read main's while the handler used these, identical today and silently divergent
the day either is tuned.
"""
import os
import json
import hashlib
import logging
import pathlib
import datetime as _dt

from fastapi import (APIRouter, Depends, File, Form, HTTPException, UploadFile)
from pydantic import BaseModel

from app.chunking import chunk_plain
from app.database import (add_document, list_sources, delete_source,
                          list_departments, list_pii_sources, get_source_ids,
                          delete_documents)
from app.db import get_session
from app.ingest_sync import (KNOWLEDGE_DIR, _WATCHED_EXTS, _sync_knowledge_dir,
                             _sync_docs, _prune_orphan_docs)
from app.jwt_auth import require_owner, require_permission
from app.logger import log, log_error
from app.metrics import increment
from app.pii import scan_pii, redact_pii
from app.runtime_config import PII_SCAN_MODE

logger = logging.getLogger(__name__)

router = APIRouter()


MAX_UPLOAD_MB                = int(os.getenv("MAX_UPLOAD_MB", "50"))
# Read granularity for uploads. Bounds how far past MAX_UPLOAD_MB a rejected
# body can push memory: at most one chunk beyond the limit, not the whole file.
_UPLOAD_CHUNK_BYTES          = 1024 * 1024
class IngestRequest(BaseModel):
    doc_id: str
    text: str
    metadata: dict = {}
    department: str = "general"


@router.get("/api/admin/pii-sources")
def admin_pii_sources(current_user: dict = Depends(require_permission("manage_kb"))):
    """Return all sources flagged during PII scanning."""
    return {"sources": list_pii_sources(), "mode": PII_SCAN_MODE}


# -- Injection gate: quarantine review ----------------------------------------

@router.get("/api/admin/injection-sources")
def admin_injection_sources(current_user: dict = Depends(require_permission("manage_kb"))):
    """Sources carrying INDEXED-but-flagged chunks (tagged, not withheld)."""
    from app.database import list_injection_flagged_sources
    from app.corpus_scan import INJECTION_SCAN_MODE
    return {"sources": list_injection_flagged_sources(), "mode": INJECTION_SCAN_MODE}


@router.get("/api/admin/kb/quarantine")
def admin_list_quarantine(status: str = "held",
                          current_user: dict = Depends(require_permission("manage_kb"))):
    """Content the injection gate WITHHELD from the corpus, awaiting review.
    Owner-only decision surface (manage_kb), newest first."""
    from app.models import QuarantinedDoc
    with get_session() as db:
        q = db.query(QuarantinedDoc)
        if status:
            q = q.filter(QuarantinedDoc.status == status)
        rows = q.order_by(QuarantinedDoc.id.desc()).all()
        return {"items": [
            {"id": r.id, "source": r.source, "department": r.department,
             "trust_tier": r.trust_tier,
             "findings": json.loads(r.findings) if r.findings else [],
             "text_preview": (r.text or "")[:1000], "text_length": len(r.text or ""),
             "status": r.status, "created_at": r.created_at,
             "reviewed_at": r.reviewed_at}
            for r in rows
        ]}


@router.post("/api/admin/kb/quarantine/{item_id}/release")
def admin_release_quarantine(item_id: int,
                             current_user: dict = Depends(require_permission("manage_kb"))):
    """Owner override: re-ingest a held document into the corpus. The
    injection tag is PRESERVED (audit), only the withholding is waived
    (quarantine_exempt) - so a released doc is still labeled untrusted at
    retrieval and still governed by the data-not-instructions prompt rules.
    Owner-role only: releasing untrusted content is a trust decision, not a
    content-management one."""
    from app.permissions import is_owner
    if not is_owner(current_user):
        raise HTTPException(status_code=403, detail="Only the owner can release quarantined content.")
    from app.models import QuarantinedDoc
    from app.corpus_scan import finding_types
    with get_session() as db:
        row = db.get(QuarantinedDoc, item_id)
        if not row or row.status != "held":
            raise HTTPException(status_code=404, detail="No held quarantine item with that id.")
        source, department, text = row.source, row.department, row.text
        findings = json.loads(row.findings) if row.findings else []
    # Re-ingest OUTSIDE the txn (embedding is slow); tag preserved, block
    # waived.
    #
    # The row is NOT marked released yet. It used to be flipped inside the
    # block above, which commits on exit - so an embed or upsert failure below
    # left the review record saying "released" with the content deleted and
    # never re-indexed, and this path had no exception handling at all. The
    # status is a claim about the corpus, so it may only be written once the
    # corpus actually says it.
    chunks = chunk_plain(text)
    meta = {"trust": "untrusted", "injection_flagged": "true",
            "injection_types": finding_types(findings)}
    # Add-then-prune, same reasoning as the upload path: never leave a window
    # where the source is absent from the index.
    desired: dict[str, tuple[int, str]] = {}
    for i, chunk in enumerate(chunks):
        doc_id = hashlib.md5(f"{department}::{source}::{chunk}".encode(),
                             usedforsecurity=False).hexdigest()
        desired.setdefault(doc_id, (i, chunk))
    existing = set(get_source_ids(source, department))
    added_ids: list[str] = []
    try:
        for doc_id, (i, chunk) in desired.items():
            if doc_id in existing:
                continue
            add_document(doc_id, chunk, {"source": source, "chunk": i, **meta},
                         department=department, quarantine_exempt=True)
            added_ids.append(doc_id)
        stale = sorted(existing - desired.keys())
        if stale:
            delete_documents(stale, department)
    except Exception as e:
        # Stay held and say why. A failed release that reports success is the
        # worst outcome here: the operator believes reviewed content is live
        # and searchable when it is neither.
        #
        # And UNDO the adds. "Held" is a claim that this content is not
        # searchable; add-then-prune means a failure in the prune leaves the
        # new chunks already indexed, so without this rollback the panel would
        # say held while the quarantined text was live in retrieval - the
        # exact inversion the quarantine exists to prevent. Add-then-prune is
        # the right order for durability of content the user WANTS indexed;
        # withheld content wants the opposite guarantee.
        if added_ids:
            try:
                delete_documents(added_ids, department)
            except Exception:
                logger.exception(
                    "quarantine release rollback FAILED for %s - %d chunks may be "
                    "indexed while the item reads held", source, len(added_ids))
        with get_session() as db:
            r = db.get(QuarantinedDoc, item_id)
            if r:
                r.release_error = str(e)[:500]
        log_error("quarantine_release_failed", quarantine_id=item_id,
                  source=source, admin_id=current_user["id"], error=str(e))
        raise HTTPException(status_code=500,
                            detail="Release failed; item remains held. See logs.")
    # Indexed. Only now is it released.
    with get_session() as db:
        r = db.get(QuarantinedDoc, item_id)
        if r:
            r.status = "released"
            r.reviewed_at = _dt.datetime.utcnow().isoformat()
            r.release_error = None
    log("quarantine_released", quarantine_id=item_id, source=source,
        admin_id=current_user["id"], chunks=len(chunks))
    return {"status": "released", "source": source, "chunks": len(chunks)}


@router.delete("/api/admin/kb/quarantine/{item_id}")
def admin_delete_quarantine(item_id: int,
                            current_user: dict = Depends(require_permission("manage_kb"))):
    """Discard held content - it was never indexed, so this just marks the
    review row deleted (the text is retained for audit unless purged)."""
    from app.models import QuarantinedDoc
    with get_session() as db:
        row = db.get(QuarantinedDoc, item_id)
        if not row:
            raise HTTPException(status_code=404, detail="No quarantine item with that id.")
        row.status = "deleted"
        row.reviewed_at = _dt.datetime.utcnow().isoformat()
    log("quarantine_deleted", quarantine_id=item_id, admin_id=current_user["id"])
    return {"status": "deleted", "id": item_id}
def _check_department_write(current_user: dict, department: str):
    """Write-authz: the Owner ingests anywhere; everyone else only 'general'
    or their own department (so an Admin can't write into an Owner-only
    collection)."""
    from app.permissions import is_owner
    if is_owner(current_user):
        return
    if department not in ("general", current_user.get("department")):
        raise HTTPException(status_code=403, detail=f"Not permitted to ingest into department: {department}")


def _ingest_trust(current_user: dict, requested: str | None = None) -> str:
    """Provenance tier for API/upload ingestion (injection gate). Owner-role
    content is the owner's own -> curated (an Owner may explicitly request a
    lower tier, e.g. a batch run under the owner account stamping
    'untrusted'); every other caller is clamped to untrusted - a non-owner
    must not be able to author policy-tier content, whatever they claim."""
    from app.permissions import is_owner
    from app.rag_config import TRUST_TIER_CURATED, TRUST_TIER_UNTRUSTED, TRUST_TIER_ORDER
    if is_owner(current_user):
        return requested if requested in TRUST_TIER_ORDER else TRUST_TIER_CURATED
    return TRUST_TIER_UNTRUSTED


from app.quarantine import write_quarantine_row as _write_quarantine_row


@router.post("/api/ingest")
def ingest(request: IngestRequest, current_user: dict = Depends(require_permission("manage_kb"))):
    from app.corpus_scan import QuarantinedContent
    _check_department_write(current_user, request.department or "general")
    text = request.text
    meta = dict(request.metadata)
    meta["trust"] = _ingest_trust(current_user, meta.get("trust"))
    if PII_SCAN_MODE != "off":
        findings = scan_pii(text)
        if findings:
            if PII_SCAN_MODE == "redact":
                text = redact_pii(text)
            meta["pii_flagged"] = "true"
            meta["pii_types"] = ",".join(f["type"] for f in findings)
            log("pii_detected", source=meta.get("source", "?"), findings=findings, mode=PII_SCAN_MODE)
    try:
        add_document(request.doc_id, text, meta, department=request.department)
    except QuarantinedContent as q:
        return _write_quarantine_row(q.source, q.department, q.trust_tier,
                                     q.text, q.findings)
    increment("ingest_total")
    log("ingest", doc_id=request.doc_id, department=request.department)
    return {"status": "ingested", "doc_id": request.doc_id}


@router.get("/api/ingest/sources")
def get_sources(department: str | None = None, current_user: dict = Depends(require_permission("manage_kb"))):
    return {"sources": list_sources(department=department)}


@router.post("/api/kb/sync")
def kb_sync(current_user: dict = Depends(require_permission("manage_kb"))):
    from datetime import datetime, timezone
    return {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "files":     _sync_knowledge_dir(),
        "docs":      _sync_docs(),
    }


@router.get("/api/kb/files")
def kb_files(current_user: dict = Depends(require_permission("manage_kb"))):
    if not os.path.isdir(KNOWLEDGE_DIR):
        return {"files": [], "directory": KNOWLEDGE_DIR}
    ingested_names = {s["source"] for s in list_sources()}
    files = []
    for p in sorted(pathlib.Path(KNOWLEDGE_DIR).iterdir()):
        if p.is_file() and p.suffix.lower() in _WATCHED_EXTS:
            stat = p.stat()
            files.append({
                "name":     p.name,
                "size":     stat.st_size,
                "modified": _dt.datetime.fromtimestamp(stat.st_mtime, tz=_dt.timezone.utc).isoformat(),
                "ingested": p.name in ingested_names,
            })
    return {"files": files, "directory": KNOWLEDGE_DIR}


@router.get("/api/ingest/departments")
def get_departments(current_user: dict = Depends(require_permission("manage_kb"))):
    return {"departments": list_departments()}


@router.delete("/api/ingest/source/{source}")
def remove_source(source: str, department: str | None = None, current_user: dict = Depends(require_permission("manage_kb"))):
    delete_source(source, department=department)
    return {"status": "deleted", "source": source, "department": department or "general"}


@router.post("/api/ingest/upload")
async def upload_file(
    file: UploadFile = File(...),
    department: str = Form("general"),
    current_user: dict = Depends(require_permission("manage_kb")),
):
    _check_department_write(current_user, department or "general")
    name = file.filename or "upload"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    # Read in chunks and stop AT the limit. `await file.read()` pulled the
    # whole body into RAM first and checked the size after, so the limit only
    # ever described files small enough not to need one - a body larger than
    # the container's memory took the process down via the OOM killer before
    # the 413 could be raised. The cap is enforced on the way in now.
    limit = MAX_UPLOAD_MB * 1024 * 1024
    buf = bytearray()
    while True:
        piece = await file.read(_UPLOAD_CHUNK_BYTES)
        if not piece:
            break
        buf.extend(piece)
        if len(buf) > limit:
            await file.close()
            raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB} MB)")
    data = bytes(buf)

    # Shared extractor - a file type behaves identically whether it arrives
    # by upload or any future batch path.
    from app.text_extract import ExtractError, extract_text
    try:
        text = extract_text(name, data)
    except ExtractError as e:
        raise HTTPException(status_code=400 if e.unsupported else 422,
                            detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to extract text: {e}")

    # Injection gate: scan the FULL document text BEFORE chunking (a payload
    # spanning a chunk boundary must not dodge the scan, and the caller must
    # learn their upload was withheld synchronously). add_document stays the
    # per-chunk backstop; the tag meta below keeps it quiet.
    from app import corpus_scan
    trust = _ingest_trust(current_user)
    inj_meta: dict = {}
    if corpus_scan.INJECTION_SCAN_MODE != "off":
        inj_findings = corpus_scan.scan(text)
        if inj_findings:
            if corpus_scan.should_quarantine(trust, inj_findings):
                return _write_quarantine_row(name, department, trust, text, inj_findings)
            inj_meta = {
                "injection_flagged": "true",
                "injection_types": corpus_scan.finding_types(inj_findings),
            }
            if trust in corpus_scan.UNTRUSTED_TIERS:
                log("injection_detected", source=name, trust=trust,
                    types=inj_meta["injection_types"], quarantined=False,
                    mode=corpus_scan.INJECTION_SCAN_MODE)

    # PII scan on full document text (before chunking for best detection
    # accuracy)
    pii_findings: list[dict] = []
    pii_meta: dict = {}
    if PII_SCAN_MODE != "off":
        pii_findings = scan_pii(text)
        if pii_findings:
            if PII_SCAN_MODE == "redact":
                text = redact_pii(text)
            pii_meta = {
                "pii_flagged": "true",
                "pii_types": ",".join(f["type"] for f in pii_findings),
            }
            log("pii_detected", source=name, findings=pii_findings, mode=PII_SCAN_MODE)

    pii_summary = None
    if PII_SCAN_MODE != "off":
        pii_summary = {"found": bool(pii_findings), "mode": PII_SCAN_MODE, "types": [f["type"] for f in pii_findings]}

    chunk_meta = {"trust": trust, **inj_meta, **pii_meta}

    chunks = chunk_plain(text)
    # ADD-THEN-PRUNE, the same set-diff _ingest_file uses. Chunk ids are
    # CONTENT-ADDRESSED - md5(dept::name::chunk-text) - so the new version's
    # chunks are written first and only the chunks whose text is genuinely gone
    # are deleted, last.
    #
    # This replaces a delete_source() that ran BEFORE the embed loop. Any
    # failure after that delete - an embed timeout, a Chroma upsert error, the
    # process dying - turned "update this document" into "delete this
    # document", and the try only caught QuarantinedContent, so ordinary
    # infrastructure failures escaped with the old copy already gone. There is
    # now no window in which the source is absent: worst case the index briefly
    # holds both versions' chunks, which retrieval reads as the same document.
    #
    # Positional ids from before this change match nothing in the desired set,
    # so a source's first upload after this ships prunes its whole old
    # generation in the same pass - one full swap, then deltas forever.
    desired: dict[str, tuple[int, str]] = {}
    for i, chunk in enumerate(chunks):
        doc_id = hashlib.md5(f"{department}::{name}::{chunk}".encode(),
                             usedforsecurity=False).hexdigest()
        desired.setdefault(doc_id, (i, chunk))
    existing = set(get_source_ids(name, department))
    try:
        for doc_id, (i, chunk) in desired.items():
            if doc_id in existing:
                continue
            add_document(doc_id, chunk, {"source": name, "chunk": i, **chunk_meta},
                         department=department)
    except corpus_scan.QuarantinedContent as q:
        # Backstop for a pattern that anchors at a chunk boundary and fires
        # chunk-level-only: drop what this upload added and quarantine the
        # WHOLE document (full text, not the chunk) instead of 500ing mid-loop.
        # The previous version stays indexed - nothing was deleted.
        added = [d for d in desired if d not in existing]
        if added:
            delete_documents(added, department)
        return _write_quarantine_row(name, department, q.trust_tier, text,
                                     q.findings)
    # Every chunk of the new version is indexed. Only now do the chunks that
    # this version no longer contains get dropped.
    stale = sorted(existing - desired.keys())
    if stale:
        delete_documents(stale, department)

    increment("ingest_total")
    log("ingest_upload", source=name, chunks=len(chunks), ext=ext, department=department)
    return {"status": "ingested", "source": name, "chunks": len(chunks), "department": department, "pii": pii_summary}
@router.post("/api/admin/kb/prune-orphans")
def prune_orphans_endpoint(current_user: dict = Depends(require_owner)):
    """Observable orphan purge: delete docs/ sources with no file on disk,
    and report exactly what was removed + which docs/ sources remain in the
    index."""
    return _prune_orphan_docs()


@router.get("/api/admin/kb/rerank-status")
def rerank_status_endpoint(current_user: dict = Depends(require_owner)):
    """Is the cross-encoder reranker actually loaded on this box, or silently
    falling back to raw similarity order? Reports the load error + a
    self-test."""
    from app.rerank import status
    return status()
