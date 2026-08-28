"""The per-answer rerank receipt: rerank_ms + rerank_pool + rerank_provider
on the audit row.

Once production rerank can ride a remote GPU scorer with a
remote->local->none fallback chain, nothing records which arm served an
answer or what it cost - unless the receipt does. These tests pin the stats
out-param's contract in rerank()/retrieve() (filled when scoring runs,
provider names the ARM that actually served, absent keys when scoring never
ran), the writer round-trip, and the wiring: every audit call site in chat
stamps all three fields.

The writer tests call log_audit_entry directly, so the ENABLE_AUDIT_LOG=false
test env never gates them - the gate lives at the chat call sites, which the
wiring test covers by source inspection.
"""
import inspect

from app.audit import log_audit_entry
from app.config import set_config
from app.db import get_session
from app.models import AuditLog

import app.rerank as rr
from tests.test_rerank_providers import (
    CANDS, LOCAL_ORDER, _PostRecorder, _clear_cfg, _fake_local, _FakeBoom,
)


# -- The stats out-param in rerank() ------------------------------------------

def test_stats_record_local_provider_and_ms(monkeypatch, client):
    try:
        _fake_local(monkeypatch)
        stats = {}
        rr.rerank("q", CANDS, top_k=2, stats=stats)
        assert stats["rerank_provider"] == "local"
        assert isinstance(stats["rerank_ms"], int) and stats["rerank_ms"] >= 0
    finally:
        _clear_cfg()


def test_stats_name_the_fallback_when_the_chain_engages(monkeypatch, client):
    """A remote-scorer nap must be VISIBLE in production, not inferred from
    log lines - that is the whole reason the receipt exists."""
    try:
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://gpu-box:9000/rerank")
        post = _PostRecorder(exc=ConnectionError("scoring box is asleep"))
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch)
        stats = {}
        out = rr.rerank("q", CANDS, top_k=2, stats=stats)
        assert [c["text"] for c in out] == LOCAL_ORDER[:2]
        assert stats["rerank_provider"] == "local-fallback"
    finally:
        _clear_cfg()


def test_stats_record_chain_end_as_none(monkeypatch, client):
    try:
        set_config("rerank_provider", "remote-http")
        set_config("rerank_remote_url", "http://gpu-box:9000/rerank")
        post = _PostRecorder(exc=ConnectionError("scoring box is asleep"))
        monkeypatch.setattr(rr, "requests", type("M", (), {"post": post}))
        _fake_local(monkeypatch, cls=_FakeBoom)
        stats = {}
        out = rr.rerank("q", CANDS, top_k=2, stats=stats)
        assert [c["text"] for c in out] == [c["text"] for c in CANDS[:2]], \
            "chain exhausted must mean retriever order"
        assert stats["rerank_provider"] == "none"
    finally:
        _clear_cfg()


def test_stats_stay_absent_when_scoring_never_runs(client):
    """Absent keys mean not-applicable - a disabled reranker must not write a
    fabricated 0ms or a provider that never served (the NULL-means-unknown
    stance: record unknown, never 0)."""
    try:
        set_config("rerank_enabled", "false")
        stats = {}
        rr.rerank("q", CANDS, top_k=2, stats=stats)
        assert "rerank_ms" not in stats and "rerank_provider" not in stats
    finally:
        _clear_cfg()


def test_retrieve_records_the_pool_size(monkeypatch, client):
    """rerank_pool = candidates handed over AFTER the per-source cap - the
    denominator that makes rerank_ms comparable across answers. Recorded even
    with rerank disabled: the pool existed either way."""
    try:
        set_config("rerank_enabled", "false")
        fake_wide = [{"text": f"c{i}", "source": f"s{i}", "score": 0.9}
                     for i in range(7)]
        import app.database as db_mod
        monkeypatch.setattr(db_mod, "query_similar",
                            lambda *a, **k: list(fake_wide))
        stats = {}
        rr.retrieve("what is the plan", stats=stats)
        assert stats["rerank_pool"] == 7
    finally:
        _clear_cfg()


# -- Writer round-trip --------------------------------------------------------

def test_receipt_columns_round_trip_through_the_writer(client):
    sid = "rerank-receipt-test"
    with get_session() as db:
        db.query(AuditLog).filter(AuditLog.session_id == sid).delete()
        db.commit()
    try:
        log_audit_entry(user_id=None, username="t", session_id=sid,
                        prompt="p", response_length=1, model="m", use_rag=True,
                        sources=[], rerank_ms=433, rerank_pool=60,
                        rerank_provider="remote-http")
        with get_session() as db:
            row = (db.query(AuditLog).filter(AuditLog.session_id == sid)
                   .order_by(AuditLog.id.desc()).first())
            assert (row.rerank_ms, row.rerank_pool, row.rerank_provider) == \
                (433, 60, "remote-http")
        # Omitting them records unknown, never 0 (the NULL-means-unknown
        # stance).
        log_audit_entry(user_id=None, username="t", session_id=sid,
                        prompt="p", response_length=1, model="m", use_rag=False,
                        sources=[])
        with get_session() as db:
            row = (db.query(AuditLog).filter(AuditLog.session_id == sid)
                   .order_by(AuditLog.id.desc()).first())
            assert row.rerank_ms is None and row.rerank_provider is None
    finally:
        with get_session() as db:
            db.query(AuditLog).filter(AuditLog.session_id == sid).delete()
            db.commit()


# -- WIRING: columns are useless if the call sites never stamp them -----------

def test_every_audit_call_site_in_chat_stamps_the_receipt():
    """Both chat audit call sites (the rag_refusal lane and the model lane)
    must read the stats dict, or one lane silently records NULLs forever
    while looking covered."""
    from app.routers.chat import chat
    src = inspect.getsource(chat)
    calls = src.count("log_audit_entry(")
    assert calls == 2, f"chat has {calls} audit call sites - update this guard"
    for field in ("rerank_ms", "rerank_pool", "rerank_provider"):
        stamps = src.count(f"{field}=_rr_stats.get(")
        assert stamps == calls, (
            f"an audit call site does not stamp {field} from _rr_stats - "
            f"that lane's receipt would silently stay NULL")
    # And retrieve() must actually be handed the dict the stamps read.
    assert "stats=_rr_stats" in src
