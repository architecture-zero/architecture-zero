import csv
import hashlib
import io
import json
from datetime import datetime, timedelta

from app.db import get_session
from app.models import AuditLog


def log_audit_entry(
    user_id: int | None,
    username: str | None,
    session_id: str,
    prompt: str,
    response_length: int,
    model: str | None,
    use_rag: bool,
    sources: list[str],
    duration_ms: int | None = None,
    ttft_ms: int | None = None,
    answer_lane: str | None = None,
    rerank_ms: int | None = None,
    rerank_pool: int | None = None,
    rerank_provider: str | None = None,
) -> None:
    """One audit row per answered turn.

    ttft_ms / answer_lane: both default to None so a caller that omits them
    records "unknown" rather than a fabricated value. The no-model lanes
    (e.g. rag_refusal) pass answer_lane and leave ttft_ms None - they never
    receive a provider token, so 0 would be a lie, not a measurement.

    rerank_*: the per-answer rerank receipt (ms / pool size / provider that
    actually served, fallback chain visible). Same NULL contract - a turn
    where retrieval never ran records unknown, never 0."""
    with get_session() as db:
        db.add(AuditLog(
            user_id=user_id,
            username=username or "anonymous",
            session_id=session_id,
            timestamp=datetime.utcnow().isoformat(),
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            prompt_preview=prompt[:200],
            response_length=response_length,
            model=model,
            use_rag=use_rag,
            sources=json.dumps(sources),
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            answer_lane=answer_lane,
            rerank_ms=rerank_ms,
            rerank_pool=rerank_pool,
            rerank_provider=rerank_provider,
        ))


def is_model_lane(lane: str | None) -> bool:
    """True unless the row is KNOWN to be model-less.

    NULL stays IN the pool: rows predating the column are unknown, and
    dropping them would silently move every historical number. Known
    contaminated rows age out of the rolling window on their own."""
    return lane is None or lane == "model"


# -- Overview-dashboard aggregates -------------------------------------------
# Percentiles derive at read time from the per-answer duration_ms rows - a
# live query, never a hand-kept number. Rows predating the column are NULL
# and simply absent from the percentile pool (unknown, not zero).

def _percentile(sorted_vals: list[int], pct: float) -> int | None:
    """Nearest-rank percentile over a pre-sorted list; None on empty input.
    Nearest-rank: the ceil(p/100 * n)-th smallest value (1-based) - p50 over
    ten values is the 5th, not the 6th."""
    if not sorted_vals:
        return None
    import math
    k = max(0, min(len(sorted_vals) - 1,
                   math.ceil(pct / 100.0 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def latency_summary(durations: list[int]) -> dict:
    """Pure core: p50/p95/p99 + count over per-answer durations (ms)."""
    vals = sorted(d for d in durations if d is not None)
    return {
        "answers_timed": len(vals),
        "p50_ms": _percentile(vals, 50),
        "p95_ms": _percentile(vals, 95),
        "p99_ms": _percentile(vals, 99),
    }


def usage_metrics(days: int = 7) -> dict:
    """Windowed usage aggregates for the Overview dashboard: answer counts,
    latency percentiles (today + window), and the per-model split."""
    now = datetime.utcnow()
    today_start = now.strftime("%Y-%m-%dT00:00:00")
    window_start = (now - timedelta(days=days)).isoformat()
    with get_session() as db:
        window_rows = (db.query(AuditLog.model, AuditLog.duration_ms,
                                AuditLog.timestamp, AuditLog.ttft_ms,
                                AuditLog.answer_lane)
                       .filter(AuditLog.timestamp >= window_start).all())
    today = [r for r in window_rows if r.timestamp >= today_start]
    # Latency and the per-model split count only rows a model actually
    # served. Deterministic lanes answer in ~100ms while still stamping the
    # requested model: left in, they drag p50 down and credit models that
    # never ran. The excluded count is REPORTED, not silently dropped -
    # answers_today/answers_window still count them.
    model_window = [r for r in window_rows if is_model_lane(r.answer_lane)]
    model_today = [r for r in today if is_model_lane(r.answer_lane)]
    models: dict[str, int] = {}
    for r in model_window:
        key = r.model or "unknown"
        models[key] = models.get(key, 0) + 1
    return {
        "window_days": days,
        "answers_today": len(today),
        "answers_window": len(window_rows),
        "answers_no_model_window": len(window_rows) - len(model_window),
        "latency_today": latency_summary([r.duration_ms for r in model_today]),
        "latency_window": latency_summary([r.duration_ms for r in model_window]),
        # Pre-token time on the same pool: retrieval + rerank + context
        # assembly + provider prefill. duration minus ttft is generation.
        "ttft_window": latency_summary([r.ttft_ms for r in model_window]),
        "models_window": dict(sorted(models.items(), key=lambda kv: -kv[1])),
    }


def get_audit_log(
    page: int = 1,
    page_size: int = 50,
    username_filter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    model_filter: str | None = None,
) -> dict:
    with get_session() as db:
        q = db.query(AuditLog)
        if username_filter:
            q = q.filter(AuditLog.username.ilike(f"%{username_filter}%"))
        if date_from:
            q = q.filter(AuditLog.timestamp >= date_from)
        if date_to:
            q = q.filter(AuditLog.timestamp <= date_to + "T23:59:59")
        if model_filter:
            q = q.filter(AuditLog.model == model_filter)

        total = q.count()
        entries = (
            q.order_by(AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
            "entries": [_to_dict(e) for e in entries],
        }


def _to_dict(e: AuditLog) -> dict:
    return {
        "id": e.id,
        "user_id": e.user_id,
        "username": e.username,
        "session_id": e.session_id,
        "timestamp": e.timestamp,
        "prompt_hash": e.prompt_hash,
        "prompt_preview": e.prompt_preview,
        "response_length": e.response_length,
        "model": e.model,
        "use_rag": e.use_rag,
        "sources": json.loads(e.sources or "[]"),
        "duration_ms": e.duration_ms,
        "ttft_ms": e.ttft_ms,
        "answer_lane": e.answer_lane,
    }


def export_audit_csv(
    date_from: str | None = None,
    date_to: str | None = None,
    username_filter: str | None = None,
) -> str:
    with get_session() as db:
        q = db.query(AuditLog)
        if username_filter:
            q = q.filter(AuditLog.username.ilike(f"%{username_filter}%"))
        if date_from:
            q = q.filter(AuditLog.timestamp >= date_from)
        if date_to:
            q = q.filter(AuditLog.timestamp <= date_to + "T23:59:59")
        rows = q.order_by(AuditLog.id.asc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "timestamp", "username", "user_id", "session_id",
        "model", "answer_lane", "use_rag", "response_length",
        "duration_ms", "ttft_ms", "prompt_preview",
        "prompt_hash", "sources",
    ])
    for r in rows:
        writer.writerow([
            r.id, r.timestamp, r.username, r.user_id, r.session_id,
            r.model, r.answer_lane, r.use_rag, r.response_length,
            r.duration_ms, r.ttft_ms, r.prompt_preview,
            r.prompt_hash, r.sources,
        ])
    return output.getvalue()


def purge_old_entries(days: int) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with get_session() as db:
        deleted = db.query(AuditLog).filter(AuditLog.timestamp < cutoff).delete()
        return deleted
