"""Guards the latency receipts on the audit trail.

(a) ttft_ms splits an answer into pre-token work vs generation, so "it is
    the RAG path, not the model" can be a measurement instead of a
    hypothesis.
(b) answer_lane separates the lane that answers WITHOUT calling a model
    (the deterministic RAG refusal) from rows a model actually served -
    otherwise fast canned rows are credited to models that never ran.

Per-model latency is not quotable until those are separable, so the
aggregate behaviour is pinned here, including the WIRING check: every audit
call site must stamp a lane, or a future extra lane silently
re-contaminates the numbers.
"""
import inspect
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from app.audit import is_model_lane, log_audit_entry, usage_metrics  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import AuditLog  # noqa: E402


# -- (b) which rows count as model-served -------------------------------------

def test_known_no_model_lanes_are_excluded():
    assert is_model_lane("rag_refusal") is False
    # Any future NAMED lane is excluded by default - only "model" (and the
    # legacy NULL) may enter the per-model pool.
    assert is_model_lane("some_future_lane") is False


def test_model_lane_counts():
    assert is_model_lane("model") is True


def test_null_lane_stays_in_the_pool_as_unknown():
    # Rows predating the lane column are unknown, not model-less. Dropping
    # them would silently move every historical number.
    assert is_model_lane(None) is True


# -- aggregate behaviour over real rows ---------------------------------------

def _seed(model, duration_ms, ttft_ms, lane, ts=None):
    log_audit_entry(
        user_id=None, username="lanetest", session_id="lane-test",
        prompt="p", response_length=10, model=model, use_rag=True,
        sources=[], duration_ms=duration_ms, ttft_ms=ttft_ms,
        answer_lane=lane,
    )
    if ts:
        with get_session() as db:
            row = (db.query(AuditLog).filter(AuditLog.session_id == "lane-test")
                   .order_by(AuditLog.id.desc()).first())
            row.timestamp = ts


def _clear():
    with get_session() as db:
        db.query(AuditLog).filter(AuditLog.session_id == "lane-test").delete()


def test_no_model_rows_do_not_drag_latency_or_credit_models():
    _clear()
    try:
        # Two real model answers, plus two no-model refusal rows answering in
        # ~100ms while stamping the model the request asked for - the live
        # contamination shape this guard exists for.
        _seed("claude-opus-5", 40000, 30000, "model")
        _seed("claude-opus-5", 60000, 50000, "model")
        _seed("claude-opus-5", 84, None, "rag_refusal")
        _seed("claude-opus-5", 107, None, "rag_refusal")

        u = usage_metrics(days=7)

        # Latency pool is the model rows only - p50 is 40s, not ~100ms.
        assert u["latency_window"]["answers_timed"] == 2
        assert u["latency_window"]["p50_ms"] == 40000
        # The per-model split credits only answers a model actually served.
        assert u["models_window"]["claude-opus-5"] == 2
        # Excluded rows are REPORTED, never silently dropped.
        assert u["answers_no_model_window"] == 2
        assert u["answers_window"] == 4
        # ttft rides the same pool.
        assert u["ttft_window"]["answers_timed"] == 2
        assert u["ttft_window"]["p50_ms"] == 30000
    finally:
        _clear()


def test_legacy_null_lane_rows_still_count_everywhere():
    _clear()
    try:
        _seed("qwen3.6:27b", 92000, None, None)  # a row predating the lane column
        u = usage_metrics(days=7)
        assert u["models_window"]["qwen3.6:27b"] == 1
        assert u["latency_window"]["answers_timed"] == 1
        assert u["answers_no_model_window"] == 0
        # NULL ttft is unknown, not zero - excluded from the ttft pool only.
        assert u["ttft_window"]["answers_timed"] == 0
    finally:
        _clear()


def test_columns_round_trip_through_the_writer():
    # exists != active: the writer must actually persist both values.
    _clear()
    try:
        _seed("claude-sonnet-5", 12345, 999, "model")
        with get_session() as db:
            row = (db.query(AuditLog).filter(AuditLog.session_id == "lane-test")
                   .order_by(AuditLog.id.desc()).first())
            assert row.ttft_ms == 999
            assert row.answer_lane == "model"
    finally:
        _clear()


def test_no_model_lanes_record_null_ttft_never_zero():
    _clear()
    try:
        _seed("claude-opus-5", 84, None, "rag_refusal")
        with get_session() as db:
            row = (db.query(AuditLog).filter(AuditLog.session_id == "lane-test")
                   .order_by(AuditLog.id.desc()).first())
            assert row.ttft_ms is None  # 0 would be a lie, not a measurement
    finally:
        _clear()


# -- WIRING: the columns are useless if the call sites never stamp them -------

def test_every_audit_call_site_in_chat_stamps_a_lane():
    from app.routers.chat import chat
    src = inspect.getsource(chat)
    calls = src.count("log_audit_entry(")
    stamps = src.count("answer_lane=")
    assert calls == 2, f"chat has {calls} audit call sites - update this guard"
    assert stamps == calls, (
        "an audit call site does not stamp answer_lane: an unlabelled lane "
        "silently re-enters the per-model latency pool")


def test_chat_stamps_both_lanes_and_captures_ttft():
    from app.routers.chat import chat
    src = inspect.getsource(chat)
    for lane in ('answer_lane="model"', 'answer_lane="rag_refusal"'):
        assert lane in src, f"missing lane stamp: {lane}"
    # TTFT is taken off the provider stream, not computed after the fact.
    assert "ttft_ms = int((time.monotonic() - _t0) * 1000)" in src
    assert "ttft_ms=ttft_ms" in src
