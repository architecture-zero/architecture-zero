"""Instance administration: config, the model matrix, context, audit, backup.

Seventh router out of main.py. Same rules: no prefix, full literal paths, guards
verbatim on the handlers, never `from app.main import ...`.

_model_config_dict arrives here from the settings commit, which deliberately
left it behind: its only two callers are the /api/admin/model-config pair, and
moving it with the settings routes would have left them raising NameError at
request time with nothing covering them.

Four imports stay FUNCTION-LOCAL inside their handlers and are NOT hoisted -
usage_metrics, list_sources, _provider_for_model, sqlite3. list_sources in
particular is module-level in app/routers/kb.py, so hoisting it here would add a
second module-level binding of a name only one handler in this file reads - and
that handler rebinds it locally anyway.
"""
import os
import json
import shutil
import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app.audit import get_audit_log, export_audit_csv
from app.config import get_config, set_config, get_all_config_masked
from app.jwt_auth import require_owner, require_permission
from app.logger import log
from app.runtime_config import (_config_or_default, _ollama_get, DEFAULT_MODEL,
                                EVAL_JUDGE_MODEL_DEFAULT, MAX_CONTEXT_TOKENS,
                                ENCRYPTION_AT_REST_VERIFIED, _DATA_DIR)

router = APIRouter()


class ContextConfigRequest(BaseModel):
    strategy: str


@router.get("/api/admin/context")
def get_context_config(current_user: dict = Depends(require_permission("manage_system"))):
    return {
        "strategy": get_config("context_strategy", "warn"),
        "max_tokens": MAX_CONTEXT_TOKENS,
        "encryption_verified": ENCRYPTION_AT_REST_VERIFIED,
    }


@router.patch("/api/admin/context")
def update_context_config(body: ContextConfigRequest, current_user: dict = Depends(require_permission("manage_system"))):
    if body.strategy not in ("warn", "summarize"):
        raise HTTPException(status_code=400, detail="strategy must be 'warn' or 'summarize'")
    set_config("context_strategy", body.strategy)
    log("config_update", key="context_strategy", value=body.strategy, admin_id=current_user["id"])
    return {"strategy": body.strategy}


@router.get("/api/admin/audit")
def admin_audit_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    username: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    model: str | None = Query(None),
    current_user: dict = Depends(require_permission("view_audit_log")),
):
    return get_audit_log(
        page=page,
        page_size=page_size,
        username_filter=username,
        date_from=date_from,
        date_to=date_to,
        model_filter=model,
    )


@router.get("/api/admin/audit/export")
def admin_audit_export(
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    username: str | None = Query(None),
    current_user: dict = Depends(require_permission("view_audit_log")),
):
    csv_content = export_audit_csv(date_from=date_from, date_to=date_to, username_filter=username)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )


@router.get("/api/overview/metrics")
def overview_metrics(current_user: dict = Depends(require_permission("manage_system"))):
    """Overview dashboard aggregates: the KB map and usage/latency numbers,
    derived live at request time - nothing hand-kept. Latency percentiles
    come from the per-answer duration_ms rows; a window with no timed answers
    reports None, never zero. Operator data -> manage_system (internal ops
    detail is never a lower tier's to read)."""
    from app.audit import usage_metrics
    from app.database import list_sources
    dept_map: dict[str, dict] = {}
    for row in list_sources(None):
        d = row.get("department") or "general"
        agg = dept_map.setdefault(d, {"department": d, "sources": 0, "chunks": 0})
        agg["sources"] += 1
        agg["chunks"] += int(row.get("count") or 0)
    departments = sorted(dept_map.values(), key=lambda a: -a["chunks"])
    return {
        "kb": {
            "departments": departments,
            "total_sources": sum(a["sources"] for a in departments),
            "total_chunks": sum(a["chunks"] for a in departments),
        },
        "usage": usage_metrics(days=7),
        "generated_at": _dt.datetime.utcnow().isoformat(),
    }


@router.get("/api/admin/config")
def admin_get_config(current_user: dict = Depends(require_permission("manage_system"))):
    # MASKED: this guard is manage_system, a permission an Owner can grant, and
    # provider credentials are not part of what it is for.
    return get_all_config_masked()


@router.patch("/api/admin/config")
def admin_set_config(body: dict, current_user: dict = Depends(require_permission("manage_system"))):
    allowed = {"system_prompt", "instance_name", "primary_color", "suggestions",
               "allow_model_selection", "allow_rag_toggle",
               "default_model", "chat_model",
               # Eval instrument pins: writer + judge for eval runs,
               # admin-settable but guarded - run_evals refuses a same-family
               # writer/judge pair regardless of what these are set to.
               "eval_answer_model", "eval_judge_model",
               # The rerank seam's per-call config keys - the whole point of
               # the seam is that an A/B or an operator flip is a config
               # change, not a restart. The hosted-egress LATCH is
               # deliberately NOT here: RERANK_HOSTED_ALLOWED is host-env
               # only, so no config write can start third-party egress.
               "rerank_enabled", "rerank_model", "rerank_provider",
               "rerank_remote_url", "rerank_hosted_vendor", "rerank_hosted_model",
               "rag_similarity_threshold",
               "default_rag_enabled", "guest_mode_enabled"}
    # Refuse unknown keys BY NAME (2026-08-27). Both skips below used to be a
    # bare `continue`: a key outside the allowlist, and a non-list `suggestions`,
    # were dropped in silence and answered 200 - and the audit line logged the
    # keys SUBMITTED rather than the keys WRITTEN, so the record agreed with the
    # caller that a discarded write had happened. A well-behaved client only
    # sends allowlisted keys, so the drop was invisible in normal use; the first
    # caller to send a new key would have gotten a silent no-op with no way to
    # find out.
    unknown = sorted(k for k in body if k not in allowed)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown config key(s): {', '.join(unknown)}")
    # Validated BEFORE the write loop (moved 2026-08-27): a mid-loop 400 left
    # every earlier key in the body already written and the audit line skipped -
    # a partial write reported as refused, the same lying-response class as the
    # bare `continue` this endpoint was cured of the same day.
    if "suggestions" in body and not isinstance(body["suggestions"], list):
        raise HTTPException(
            status_code=400,
            detail="suggestions must be a list of strings")
    # rag_similarity_threshold is read back with float() on the chat path, so a
    # value that will not parse is not a bad setting - it is an outage. One
    # typo in the admin field wrote "0.4 " or "high" and every subsequent chat
    # request 500'd until someone edited the row back, with nothing in the UI
    # to say why. Validated HERE, in the same pre-write block as suggestions,
    # so the refusal happens before anything is stored.
    if "rag_similarity_threshold" in body:
        try:
            _thr = float(body["rag_similarity_threshold"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="rag_similarity_threshold must be a number between 0 and 1")
        if not 0.0 <= _thr <= 1.0:
            raise HTTPException(
                status_code=400,
                detail="rag_similarity_threshold must be between 0 and 1")
    written = []
    for key, value in body.items():
        if key == "suggestions":
            value = json.dumps([s for s in value if isinstance(s, str) and s.strip()])
        elif key in ("allow_model_selection", "allow_rag_toggle", "default_rag_enabled", "guest_mode_enabled"):
            # NOT `"true" if value else "false"`. That is Python truthiness on
            # the raw JSON value, so the STRING "false" - and "no", and "0" -
            # are all truthy and were written as "true", inverting exactly the
            # intent an operator expressed. Harmless while these keys only drove
            # checkboxes; default_rag_enabled now decides whether retrieval runs
            # for every caller that omits use_rag, so a writer that flips an
            # operator's "off" into "on" changes what the instance serves.
            if isinstance(value, str):
                value = value.strip().lower() not in ("false", "0", "no", "off", "")
            value = "true" if value else "false"
        set_config(key, str(value))
        written.append(key)
    # written, not submitted: the log is the record of what CHANGED.
    log("admin_config_update", admin_id=current_user["id"], keys=sorted(written))
    return get_all_config_masked()


# -- Model pinning matrix -----------------------------------------------------
# Each slot reports value/default/overridden (+ effective where blank follows
# a chain) so a drifted dial is visible at a glance, and same_family_warning
# is computed at read time - the admin sees the writer/judge collision when
# DIALING it, not on the next refused eval run. Effective values mirror the
# REAL resolution chains verbatim: chat (chat route) falls to default_model;
# eval_writer falls to default_model (run_evals' own chain, NOT via
# chat_model); judge falls to EVAL_JUDGE_MODEL_DEFAULT.

class ModelConfigUpdate(BaseModel):
    default: str | None = None       # "" = reset to DEFAULT_MODEL
    chat: str | None = None          # "" = follow default
    eval_writer: str | None = None   # "" = follow default
    eval_judge: str | None = None    # "" = reset to EVAL_JUDGE_MODEL_DEFAULT


def _model_config_dict() -> dict:
    from app.providers import _provider_for_model
    default = _config_or_default("default_model", DEFAULT_MODEL)
    chat_raw = get_config("chat_model", "").strip()
    writer_raw = get_config("eval_answer_model", "").strip()
    judge = _config_or_default("eval_judge_model", EVAL_JUDGE_MODEL_DEFAULT)
    writer_effective = writer_raw or default
    try:
        same_family = _provider_for_model(writer_effective) == _provider_for_model(judge)
    except Exception:
        same_family = False
    return {
        "default": {"value": default, "default": DEFAULT_MODEL,
                    "overridden": default != DEFAULT_MODEL},
        "chat": {"value": chat_raw, "effective": chat_raw or default,
                 "default": "", "overridden": chat_raw != ""},
        "eval_writer": {"value": writer_raw, "effective": writer_effective,
                        "default": "", "overridden": writer_raw != ""},
        "eval_judge": {"value": judge, "default": EVAL_JUDGE_MODEL_DEFAULT,
                       "overridden": judge != EVAL_JUDGE_MODEL_DEFAULT},
        "same_family_warning": same_family,
    }


@router.get("/api/admin/model-config")
def admin_get_model_config(current_user: dict = Depends(require_permission("manage_system"))):
    return _model_config_dict()


@router.patch("/api/admin/model-config")
def admin_set_model_config(body: ModelConfigUpdate,
                           current_user: dict = Depends(require_permission("manage_system"))):
    if body.default is not None:
        set_config("default_model", body.default.strip() or DEFAULT_MODEL)
    if body.chat is not None:
        set_config("chat_model", body.chat.strip())
    if body.eval_writer is not None:
        set_config("eval_answer_model", body.eval_writer.strip())
    if body.eval_judge is not None:
        set_config("eval_judge_model", body.eval_judge.strip() or EVAL_JUDGE_MODEL_DEFAULT)
    log("model_config_update", admin_id=current_user["id"],
        keys=[k for k, v in (("default", body.default), ("chat", body.chat),
                             ("eval_writer", body.eval_writer),
                             ("eval_judge", body.eval_judge)) if v is not None])
    return _model_config_dict()


@router.get("/api/admin/models")
def admin_get_models(current_user: dict = Depends(require_permission("manage_system"))):
    try:
        data = _ollama_get("/api/tags", timeout=5).json()
        models = [m["name"] for m in data.get("models", [])]
    except Exception:
        models = []
    return {"models": models}


# -- Admin backup -------------------------------------------------------------

_BACKUP_DIR           = os.path.join(_DATA_DIR, "backups")
_BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))


@router.get("/api/admin/backup/status")
def admin_backup_status(current_user: dict = Depends(require_owner)):
    return {
        "last_backup": get_config("last_backup_timestamp", None),
        "last_backup_file": get_config("last_backup_file", None),
    }


@router.post("/api/admin/backup")
def run_backup(current_user: dict = Depends(require_owner)):
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_name = f"az_backup_{timestamp}"
    os.makedirs(_BACKUP_DIR, exist_ok=True)

    stage_dir = os.path.join(_BACKUP_DIR, f"_stage_{timestamp}")
    os.makedirs(stage_dir, exist_ok=True)
    try:
        for item in os.listdir(_DATA_DIR):
            if item == "backups":
                continue
            src = os.path.join(_DATA_DIR, item)
            dst = os.path.join(stage_dir, item)
            # WAL databases must NOT be copied as loose files while writers
            # are live: db + -wal + -shm are three copy2 calls at three
            # instants, and the restored trio can be inconsistent or drop
            # the WAL tail (SQLite documents this). The sqlite backup API
            # takes a consistent snapshot of a live database instead; the
            # sidecars are then redundant and deliberately skipped.
            if item.endswith(("-wal", "-shm")):
                continue
            # `.sqlite3` is here because CHROMA_PATH resolves to this same
            # directory, and chroma names its store chroma.sqlite3. Matching
            # only `.db` sent it down the copy2 branch below - copied live and
            # mid-write, while the skip above dropped the -wal holding its most
            # recent commits. That backup restored to a torn index missing its
            # tail, and nothing said so until a restore was attempted.
            if item.endswith((".db", ".sqlite3")):
                import sqlite3
                src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
                dst_conn = sqlite3.connect(dst)
                try:
                    with dst_conn:
                        src_conn.backup(dst_conn)
                finally:
                    src_conn.close()
                    dst_conn.close()
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        archive_path = shutil.make_archive(
            os.path.join(_BACKUP_DIR, archive_name), "gztar", stage_dir
        )
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    cutoff = _dt.datetime.now() - _dt.timedelta(days=_BACKUP_RETENTION_DAYS)
    for fname in os.listdir(_BACKUP_DIR):
        if not (fname.startswith("az_backup_") and fname.endswith(".tar.gz")):
            continue
        fpath = os.path.join(_BACKUP_DIR, fname)
        if os.path.getmtime(fpath) < cutoff.timestamp() and fpath != archive_path:
            os.remove(fpath)

    now_iso = _dt.datetime.now().isoformat()
    set_config("last_backup_timestamp", now_iso)
    set_config("last_backup_file", os.path.basename(archive_path))
    log("backup_created", admin=current_user["username"], file=os.path.basename(archive_path))
    return {
        "status": "completed",
        "file": os.path.basename(archive_path),
        "timestamp": now_iso,
        "size_bytes": os.path.getsize(archive_path),
    }
