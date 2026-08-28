"""System, health, monitoring, and trust-panel routes.

The first router extracted from main.py, so it sets the pattern for the other
85 routes: no prefix, full literal paths, guards carried verbatim, shared names
from app.runtime_config, and never `from app.main import ...`.

NO prefix= and NO router-level dependencies=[]. Seven of these paths are listed
in app/auth.py EXCLUDED_PATHS, which matches the literal request.url.path - a
prefix would silently un-exclude them and unauthenticated probers would start
getting 401s in production while the ENABLE_AUTH=false suite stayed green. A
router-level dependency would just as silently protect the six routes that are
anonymous by design.
"""
import os
import json
import logging
import shutil
import datetime as _dt

import requests
from fastapi import APIRouter, Depends, Response

from app.database import count_documents
from app.config import get_config
from app.jwt_auth import get_current_user, require_owner, require_permission
from app.agent import get_tool_config
# OLLAMA_BASE comes from providers, NOT from a copy of main.py's line 50 - the
# two had different defaults and providers' is the one that was winning.
from app.providers import (OLLAMA_BASE, OPENAI_COMPAT, compat_key_configured,
                           get_provider_config)
from app.redis_client import redis_status
from app.security import get_security_config
from app.metrics import get_last_request_at, get_snapshot, prometheus_text
from app.alerting import (fire as fire_alert, get_config as get_alert_config,
                          DISK_ALERT_THRESHOLD_PCT)
from app import corpus_scan as _corpus_scan
from app.runtime_config import (_config_or_default, _ollama_get, DEFAULT_MODEL,
                                RAG_ONLY_MODE, PII_SCAN_MODE, ALLOW_GUEST_MODE,
                                DEMO_DAILY_GUEST_LIMIT,
                                ENCRYPTION_AT_REST_VERIFIED, _DATA_DIR)

router = APIRouter()


@router.get("/")
def read_root():
    return {"status": "online", "message": "Architecture Zero API is running."}

@router.get("/api/version")
def version():
    """Public build identity - the git SHA baked in at image-build time
    (Dockerfile ARG GIT_SHA, set from `git rev-parse` in the deploy
    workflow). Lets deploy-verify confirm the LIVE commit directly instead of
    inferring it from CI. 'unknown' = built without the build-arg (e.g. local
    dev)."""
    return {"sha": os.getenv("GIT_SHA", "unknown"), "service": "architecture-zero", "api_version": "1.0"}

@router.get("/api/health")
def health():
    try:
        requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        return {"status": "healthy", "ollama": "connected"}
    except Exception:
        return {"status": "degraded", "ollama": "unreachable"}

@router.get("/api/health/ready")
def health_ready():
    """Readiness probe - checks DB, Redis, and Ollama. Returns 503 if any
    critical check fails."""
    checks: dict[str, str] = {}
    ready = True

    # DB check (critical)
    try:
        from app.db import get_session
        from sqlalchemy import text as _text
        with get_session() as s:
            s.execute(_text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        # /api/health/ready is UNAUTHENTICATED (auth.py EXCLUDED_PATHS) so the
        # monitoring prober can reach it - which makes an interpolated exception
        # disclosure to anyone. Body says pass/fail; detail goes to the log.
        logging.getLogger("uvicorn.error").error("readiness: db check failed: %s", e)
        checks["db"] = "error"
        ready = False

    # Redis check (non-critical - optional)
    _redis_url = os.getenv("REDIS_URL", "")
    if _redis_url:
        try:
            import redis as _redis
            r = _redis.from_url(_redis_url, socket_connect_timeout=2)
            r.ping()
            checks["redis"] = "ok"
        except Exception as e:
            logging.getLogger("uvicorn.error").warning("readiness: redis check failed: %s", e)
            checks["redis"] = "error"
            # Redis failure is not fatal - backend falls back to DB-only mode
    else:
        checks["redis"] = "skipped"

    # Ollama check (non-critical - optional provider)
    _enable_ollama = os.getenv("ENABLE_OLLAMA", "true").lower() == "true"
    if _enable_ollama:
        try:
            _ollama_get("/api/tags", timeout=3)
            checks["ollama"] = "ok"
        except Exception:
            checks["ollama"] = "unreachable"
            # Ollama down is degraded, not fatal - cloud providers may still
            # work
    else:
        checks["ollama"] = "skipped"

    if not ready:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"ready": False, "checks": checks})
    return {"ready": True, "checks": checks}

@router.get("/api/status", dependencies=[Depends(get_current_user)])
def status():
    ollama_ok = False
    loaded_models = []
    try:
        tags = _ollama_get("/api/tags", timeout=5).json()
        ollama_ok = True
        loaded_models = [m["name"] for m in tags.get("models", [])]
    except Exception:
        pass

    try:
        doc_count = count_documents()
    except Exception:
        doc_count = 0

    return {
        "ollama": "connected" if ollama_ok else "unreachable",
        "models_available": loaded_models,
        "rag_documents": doc_count,
        # Read from the module that actually gates - a posture surface that
        # re-derives the flag can disagree with enforcement.
        "auth_enabled": __import__("app.auth", fromlist=["ENABLE_AUTH"]).ENABLE_AUTH,
        "rag_only_mode": RAG_ONLY_MODE,
        "instance_name": os.getenv("VITE_INSTANCE_NAME", "Architecture Zero"),
        "redis": redis_status(),
        "agent_tools": get_tool_config(),
        "security": get_security_config(),
        # Guest wallet backstop. It is fail-open by design (0 = inert), so
        # without a positive signal an operator cannot tell "off" from "on" in
        # a running deployment - the same reason injection_scan_mode is here.
        "guest_daily_limit": DEMO_DAILY_GUEST_LIMIT,
        "provider": get_provider_config(),
        "pii_scan_mode": PII_SCAN_MODE,
        # Corpus injection gate (distinct from security.injection_protection,
        # which screens the USER's prompt). This one screens content ENTERING
        # the corpus and is the positive signal that the gate is live - a
        # fail-open control is silent when off, so it gets a status surface.
        "injection_scan_mode": _corpus_scan.INJECTION_SCAN_MODE,
        "encryption_verified": ENCRYPTION_AT_REST_VERIFIED,
    }

# -- Backup status probe ------------------------------------------------------
# Host-side backup jobs write backup-status.json / drill-status.json into the
# data dir (bind mount). An uptime check probes this endpoint and alerts on
# non-200. Unauthenticated by design (auth EXCLUDED_PATHS): the prober has no
# JWT, and the body discloses only ok/age/reason. Missing, stale, or failed
# status = 503 - a backup job that silently stops running MUST alarm (guards
# fail LOUD).

BACKUP_STATUS_DIR = os.getenv("BACKUP_STATUS_DIR", "/app/data")
BACKUP_MAX_AGE_HOURS = float(os.getenv("BACKUP_MAX_AGE_HOURS", "30"))


def _backup_job_state(fname: str) -> dict:
    path = os.path.join(BACKUP_STATUS_DIR, fname)
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {"ok": False, "age_hours": None, "reason": "status file missing/unreadable"}
    last = data.get("last_success")
    if not last:
        return {"ok": False, "age_hours": None, "reason": "never succeeded"}
    try:
        ts = _dt.datetime.strptime(last, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return {"ok": False, "age_hours": None, "reason": "unparseable last_success"}
    age_h = round((_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() / 3600, 1)
    if age_h > BACKUP_MAX_AGE_HOURS:
        return {"ok": False, "age_hours": age_h, "reason": f"stale (>{BACKUP_MAX_AGE_HOURS:g}h)"}
    if not data.get("ok"):
        # most recent run failed even though an older success is still fresh -
        # alarm now, don't wait for the success to age out
        return {"ok": False, "age_hours": age_h, "reason": "last run failed"}
    return {"ok": True, "age_hours": age_h}

@router.get("/api/backup-status")
def backup_status():
    backup = _backup_job_state("backup-status.json")
    drill = _backup_job_state("drill-status.json")
    body = {"ok": backup["ok"] and drill["ok"], "backup": backup, "drill": drill}
    if not body["ok"]:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=body)
    return body

@router.get("/api/config", dependencies=[Depends(get_current_user)])
def public_config():
    """Instance branding + usage-control config. Covered by AuthMiddleware
    when ENABLE_AUTH=true (not in EXCLUDED_PATHS)."""
    raw = get_config("suggestions", "[]")
    try:
        suggestions = json.loads(raw)
    except Exception:
        suggestions = []
    return {
        "instance_name":         get_config("instance_name", "Architecture Zero"),
        "primary_color":         get_config("primary_color",  "#2563eb"),
        "suggestions":           suggestions,
        "allow_model_selection": get_config("allow_model_selection", "true") == "true",
        "allow_rag_toggle":      get_config("allow_rag_toggle", "true") == "true",
        "default_model":         _config_or_default("default_model", DEFAULT_MODEL),
        # What a chat request with no explicit model actually gets (chat_model
        # pin, else default) - the client displays this instead of keeping its
        # own copy that silently bypasses the server pins.
        "chat_model_effective":  get_config("chat_model", "").strip()
                                 or _config_or_default("default_model", DEFAULT_MODEL),
        "default_rag_enabled":   get_config("default_rag_enabled", "false") == "true",
        "guest_mode_enabled":    ALLOW_GUEST_MODE and get_config("guest_mode_enabled", "false") == "true",
    }

# -- Monitoring & Alerting ----------------------------------------------------

_OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
_INSTANCE_NAME = os.getenv("VITE_INSTANCE_NAME", "Architecture Zero")

@router.get("/api/health/detailed")
def health_detailed(current_user: dict = Depends(require_owner)):
    import time as _time
    result: dict = {}

    # Disk usage
    try:
        usage = shutil.disk_usage(_DATA_DIR)
        disk_pct = round(usage.used / usage.total * 100, 1)
        result["disk"] = {
            "used_gb":  round(usage.used  / 1e9, 2),
            "total_gb": round(usage.total / 1e9, 2),
            "pct": disk_pct,
            "ok": disk_pct < DISK_ALERT_THRESHOLD_PCT,
        }
        if disk_pct >= DISK_ALERT_THRESHOLD_PCT:
            fire_alert("disk_high", f"Disk usage high - {_INSTANCE_NAME}",
                       f"Disk at {disk_pct}% ({result['disk']['used_gb']} GB used)")
    except Exception as e:
        result["disk"] = {"error": str(e)}

    # DB response time
    try:
        from app.db import get_session
        from app import models as _models  # noqa: F401 - ensure ORM is loaded
        from sqlalchemy import text as _text
        t0 = _time.perf_counter()
        with get_session() as s:
            s.execute(_text("SELECT 1"))
        result["db_ms"] = round((_time.perf_counter() - t0) * 1000, 2)
    except Exception as e:
        result["db_ms"] = None
        result["db_error"] = str(e)

    # Provider health
    providers = []
    _enable_ollama    = os.getenv("ENABLE_OLLAMA",    "true").lower()  == "true"
    _enable_openai    = os.getenv("ENABLE_OPENAI",    "false").lower() == "true"
    _enable_anthropic = os.getenv("ENABLE_ANTHROPIC", "false").lower() == "true"

    if _enable_ollama:
        try:
            t0 = _time.perf_counter()
            r = _ollama_get("/api/tags", timeout=3)
            latency = round((_time.perf_counter() - t0) * 1000, 2)
            providers.append({"name": "ollama", "ok": r.status_code == 200, "latency_ms": latency})
        except Exception:
            providers.append({"name": "ollama", "ok": False, "latency_ms": None})
            fire_alert("ollama_down", f"Ollama unreachable - {_INSTANCE_NAME}",
                       "Ollama did not respond within 3s. Chat will fail for Ollama models.")
    if _enable_openai:
        providers.append({"name": "openai", "ok": bool(os.getenv("OPENAI_API_KEY")), "latency_ms": None})
    if _enable_anthropic:
        providers.append({"name": "anthropic", "ok": bool(os.getenv("ANTHROPIC_API_KEY")), "latency_ms": None})
    # Keyed registry providers ("ok" = key present, same semantic as openai
    # above).
    for _name in OPENAI_COMPAT:
        if _name != "openai" and compat_key_configured(_name):
            providers.append({"name": _name, "ok": True, "latency_ms": None})

    result["providers"] = providers

    # Last chat request
    last = get_last_request_at()
    result["last_request_at"] = (
        _dt.datetime.fromtimestamp(last).isoformat() if last else None
    )

    result["otel_configured"] = bool(_OTEL_ENDPOINT)
    result["alerts"]  = get_alert_config()
    result["metrics"] = get_snapshot()
    return result

@router.get("/metrics", dependencies=[Depends(get_current_user)])
def metrics_endpoint():
    return Response(content=prometheus_text(), media_type="text/plain; version=0.0.4; charset=utf-8")

# -- Public trust panel --------------------------------------------------------

@router.get("/api/trust")
def trust_panel_public():
    """The public measured-trust panel (auth EXCLUDED_PATHS by design - the
    point is that visitors see it). Every number derives live from stored
    eval rows: per-corpus, band-not-point, honesty never blended, zero
    hand-set values. The public variant carries no model names and no
    deficit list."""
    from app.trust_panel import derive_trust_panel
    return derive_trust_panel(admin=False)

@router.get("/api/admin/trust")
def trust_panel_admin(current_user: dict = Depends(require_permission("view_analytics"))):
    """The operator variant: same derivation, plus provenance and working
    bands behind auth."""
    from app.trust_panel import derive_trust_panel
    return derive_trust_panel(admin=True)
