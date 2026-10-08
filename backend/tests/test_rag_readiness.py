"""The retrieval lane, proven (AZ-02, outside review 2026-10-06; fixed 2026-10-07).

THE GAP. /api/health/ready proved the database and nothing else. Retrieval gets
its embeddings from EMBED_BASE, a separate service from the chat provider, so an instance with a healthy database and a
healthy cloud chat provider read ready while every governed query failed - and
the monitors that probe readiness stayed quiet.

The review's closure list, each a test below: a healthy database and cloud chat
with a broken embed lane reads not ready; so does a missing or a wrong embed
model, a stale background check, and a vector store that cannot be read. Then
the guard that keeps the fix from becoming the outage: the readiness request
itself never reaches the embed service or the vector store.

Every case that runs the real embed call answers requests.post itself, so no
test here reaches a live embed server, whatever this machine is running. The
headline and stale tests fail on the code before the fix for the behavioral
reason (ready, no lane check); the others call the new probe and fail there on
the missing API; the never-probes test passes on both by design - it pins the
rule the fix must keep.

R-AZ-01 (the outside re-review, 2026-10-07): readiness is proof, never its
absence. "pending", "off" and "skipped" used to pass, so a broken lane read
ready from boot until the first pass, and forever with the timer off; this
file's own pending and off tests asserted 200. Now only a fresh pass that found
the lane ok, or not required, is ready, and the first pass runs at boot - its
tests fail on the code before it (pending, off and unwired answered 200; the
first pass came one interval in).
"""
import asyncio
import inspect
import threading
import json
import time

import pytest
import requests

from app import database as db
from app import self_check as sc

_BOUND = 2 * 300 + 60   # two intervals and a minute, at the 300 s interval
_HELP = getattr(db, "HELP_COLLECTION", None)


class _Col:
    def __init__(self, name="knowledge_base", n=3, query_error=None):
        self.name, self._n, self._query_error = name, n, query_error
        self.queried, self.included = [], []

    def count(self):
        return self._n

    def query(self, query_embeddings, n_results, include=None):
        self.queried.append(len(query_embeddings[0]))
        self.included.append(include)
        if self._query_error:
            raise self._query_error
        return {"ids": [["x"]]}


class _Client:
    def __init__(self, cols=(), error=None):
        self._cols, self._error = list(cols), error

    def list_collections(self):
        if self._error:
            raise self._error
        return self._cols


def _response(status, body):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.url = "http://embed.test/api/embeddings"
    return r


def _refused(*a, **k):
    raise requests.ConnectionError("connection refused")


@pytest.fixture
def lane(monkeypatch):
    """The timer started at the 300 s interval with the probe wired, no pass
    yet; alerts recorded instead of sent; the embed retry's pause skipped."""
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 300)
    monkeypatch.setattr(sc, "_rag_wired", True, raising=False)
    monkeypatch.setattr(sc, "_started_at", time.time(), raising=False)
    monkeypatch.setattr(sc, "_rag_last", None, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # Readiness's other checks must not decide or slow these tests: the chat
    # provider is not under test (its /api/tags read waits out a 3 s timeout
    # where nothing answers), and where the crash-loop check exists, a
    # workstation's test boots read as a loop.
    monkeypatch.setenv("ENABLE_OLLAMA", "false")
    try:
        import app.boot_history as _boot_history
        monkeypatch.setattr(_boot_history, "crash_loop_state", lambda: {"looping": False})
    except ImportError:
        pass
    fired = []
    monkeypatch.setattr(sc, "fire_alert", lambda key, *a, **k: fired.append(key))
    return fired


def _timer_pass(data_dir, rag_only=False, **probe_kw):
    """What the self-check timer runs, once. On a build without the lane probe
    it checks nothing about retrieval - which is the gap."""
    if "rag_probe" in inspect.signature(sc.run_self_check).parameters:
        return sc.run_self_check(
            data_dir, rag_probe=lambda: db.probe_retrieval_lane(rag_only, **probe_kw))
    return sc.run_self_check(data_dir)


def _ready(client):
    r = client.get("/api/health/ready")
    return r.status_code, r.json()["checks"]


def test_the_reviews_case_a_broken_embed_lane_is_not_ready(
        client, monkeypatch, lane, real_embed, tmp_path):
    """Healthy database, cloud chat (Ollama off), a corpus to serve, and an
    embed service nothing answers."""
    monkeypatch.setenv("ENABLE_OLLAMA", "false")
    monkeypatch.setattr(db, "client", _Client([_Col()]))
    monkeypatch.setattr(requests, "post", _refused)
    _timer_pass(str(tmp_path))
    status, checks = _ready(client)
    assert status == 503, checks
    assert checks["db"] == "ok" and checks.get("rag") == "error"
    assert "rag_down" in lane
    assert sc.rag_status()["reason"] == "embed_unreachable"


@pytest.mark.parametrize("code,reason", [(404, "embed_model_missing"), (500, "embed_refused")])
def test_a_missing_embed_model_is_not_ready(
        client, monkeypatch, lane, real_embed, tmp_path, code, reason):
    monkeypatch.setattr(db, "client", _Client([_Col()]))
    monkeypatch.setattr(requests, "post", lambda *a, **k: _response(
        code, {"error": 'model "nomic-embed-text" not found, try pulling it first'}))
    _timer_pass(str(tmp_path))
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "error"
    assert sc.rag_status()["reason"] == reason


def test_a_wrong_width_embed_model_is_not_ready(client, monkeypatch, lane, tmp_path):
    """A different model answers - with a vector the index was not built at.
    The search rejects it, as it would reject every query."""
    col = _Col(query_error=ValueError("Embedding dimension 1024 does not match 768"))
    monkeypatch.setattr(db, "client", _Client([col]))
    monkeypatch.setattr(db, "_embed", lambda *a, **k: [0.1] * 1024)
    _timer_pass(str(tmp_path))
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "error"
    assert sc.rag_status()["reason"] == "vector_search_failed"
    assert col.queried == [1024]


def test_a_vector_store_that_cannot_be_read_is_not_ready(client, monkeypatch, lane, tmp_path):
    monkeypatch.setattr(db, "client", _Client(error=RuntimeError("segment unreadable")))
    _timer_pass(str(tmp_path))
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "error"
    assert sc.rag_status()["reason"] == "vector_store_unreadable"


def test_a_stale_pass_is_not_ready_even_when_it_said_ok(client, monkeypatch, lane):
    """The timer stopped, or a pass hung: the last word was ok, but nobody has
    proven the lane for longer than the bound."""
    monkeypatch.setattr(sc, "_rag_last",
                        {"state": "ok", "reason": None, "at": time.time() - _BOUND - 5},
                        raising=False)
    status, checks = _ready(client)
    assert status == 503 and checks.get("rag") == "stale", checks
    monkeypatch.setattr(sc, "_rag_last",
                        {"state": "ok", "reason": None, "at": time.time() - _BOUND + 5})
    status, checks = _ready(client)
    assert status == 200 and checks["rag"] == "ok"


def test_before_the_first_pass_pending_then_stale(client, monkeypatch, lane):
    """No pass yet proves nothing: pending is not ready (R-AZ-01)."""
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "pending"
    monkeypatch.setattr(sc, "_started_at", time.time() - _BOUND - 5)
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "stale"


def test_a_healthy_lane_is_ready_and_searched_with_the_query_legs_vector(
        client, monkeypatch, lane, real_embed, tmp_path):
    col = _Col()
    # Where product help pages exist they come first and hold the most: they
    # are not the operator's corpus, so the search must go to the operator's.
    cols = ([_Col(name=_HELP, n=40)] if _HELP else []) + [col]
    monkeypatch.setattr(db, "client", _Client(cols))
    sent = []

    def _post(url, json=None, timeout=None, **k):
        sent.append((url, json, timeout))
        return _response(200, {"embedding": [0.25] * 768})
    monkeypatch.setattr(requests, "post", _post)
    _timer_pass(str(tmp_path))
    status, checks = _ready(client)
    assert status == 200 and checks["rag"] == "ok"
    url, body, timeout = sent[0]
    assert url.endswith("/api/embeddings") and body["prompt"] == db._LANE_PROBE_TEXT
    assert body.get("model")
    assert timeout == 15   # the probe's own bound, not the ingest's 60
    assert col.queried == [768]
    assert col.included == [["distances"]]   # no corpus text pulled into the probe
    assert "rag_down" not in lane


def test_no_operator_corpus_asks_nothing_of_the_lane(client, monkeypatch, lane, tmp_path):
    monkeypatch.setattr(db, "client", _Client([_Col(name=_HELP, n=40)] if _HELP else []))
    called = []
    monkeypatch.setattr(db, "_embed", lambda *a, **k: called.append(1) or [0.0])
    _timer_pass(str(tmp_path))
    status, checks = _ready(client)
    assert status == 200 and checks["rag"] == "not_required"
    assert called == []


def test_an_empty_store_where_a_corpus_is_expected_is_not_ready(
        client, monkeypatch, lane, tmp_path):
    """The security read's case: a store that came back empty (a volume not
    mounted, an index wiped) read not_required and readiness stayed green.
    A deployment that declares its corpus is never empty reads it as an error,
    without asking the embed service."""
    monkeypatch.setattr(db, "client", _Client([]))
    called = []
    monkeypatch.setattr(db, "_embed", lambda *a, **k: called.append(1) or [0.0])
    _timer_pass(str(tmp_path), corpus_expected=True)
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "error"
    assert sc.rag_status()["reason"] == "vector_store_empty" and called == []


def test_rag_only_mode_requires_the_lane_with_no_corpus(
        client, monkeypatch, lane, real_embed, tmp_path):
    monkeypatch.setattr(db, "client", _Client([]))
    monkeypatch.setattr(requests, "post", _refused)
    _timer_pass(str(tmp_path), rag_only=True)
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "error"
    assert sc.rag_status()["reason"] == "embed_unreachable"


def test_a_crashing_probe_reads_error_not_silence(client, monkeypatch, lane, tmp_path):
    sc.run_self_check(str(tmp_path), rag_probe=lambda: 1 / 0)
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "error"
    assert sc.rag_status()["reason"] == "probe_crashed" and "rag_down" in lane


def test_the_timer_off_reads_off_and_is_not_ready(client, monkeypatch, lane):
    """With the timer off nothing ever proves the lane, so readiness cannot
    vouch for it - indefinitely green was the gap (R-AZ-01)."""
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 0)
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "off"


def test_no_wired_probe_is_not_ready(client, monkeypatch, lane):
    monkeypatch.setattr(sc, "_rag_wired", False)
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "skipped"


def test_the_oldest_pass_accepted_is_two_intervals_and_a_minute(client, monkeypatch, lane):
    """The documented bound, both sides of it: 660 s at the default interval."""
    assert sc._stale_after() == 660
    for age, expected in ((659, 200), (661, 503)):
        monkeypatch.setattr(sc, "_rag_last", {"state": "ok", "reason": None, "at": time.time() - age})
        assert _ready(client)[0] == expected, age


def _run_loop_until(monkeypatch, probe, done, seconds=5.0):
    """The real timer at the 300 s interval with only the lane wired, until
    `done()` or the deadline; then stopped."""
    async def go():
        task = asyncio.create_task(sc.self_check_loop(".", None, None, rag_probe=probe))
        end = time.monotonic() + seconds
        while time.monotonic() < end and not done():
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(go())


def test_the_first_lane_pass_runs_at_boot_not_an_interval_later(client, monkeypatch, lane):
    calls = []
    _run_loop_until(monkeypatch, lambda: calls.append(1) or {"state": "ok"}, lambda: bool(calls))
    assert calls, "no pass within seconds of the timer starting at a 300 s interval"
    status, checks = _ready(client)
    assert status == 200 and checks["rag"] == "ok"


def test_a_dead_embed_service_is_not_ready_from_boot_and_the_boot_pass_is_quiet(
        client, monkeypatch, lane, real_embed, tmp_path):
    monkeypatch.setattr(db, "client", _Client([_Col()]))
    monkeypatch.setattr(requests, "post", _refused)
    passes = []

    def probe():
        passes.append(1)
        return db.probe_retrieval_lane(False)
    _run_loop_until(monkeypatch, probe, lambda: bool(passes) and sc._rag_last is not None)
    status, checks = _ready(client)
    assert status == 503 and checks["rag"] == "error"
    assert lane == [], "the boot pass alerted"


def test_a_failing_boot_pass_is_retried_until_the_lane_comes_up(client, monkeypatch, lane):
    monkeypatch.setattr(sc, "BOOT_RETRY_SECONDS", 0.01)
    answers = [{"state": "error", "reason": "embed_unreachable"}] * 2 + [{"state": "ok"}]
    seen = []

    def probe():
        seen.append(1)
        return answers[min(len(seen), len(answers)) - 1]
    _run_loop_until(monkeypatch, probe, lambda: len(seen) >= 3)
    assert len(seen) >= 3
    status, checks = _ready(client)
    assert status == 200 and checks["rag"] == "ok"
    assert lane == []


def test_a_stalled_boot_pass_is_not_ready(client, monkeypatch, lane):
    release, started = threading.Event(), threading.Event()

    def probe():
        started.set()
        release.wait(5)
        return {"state": "ok"}

    async def go():
        task = asyncio.create_task(sc.self_check_loop(".", None, None, rag_probe=probe))
        await asyncio.to_thread(started.wait, 5)
        status, checks = await asyncio.to_thread(_ready, client)
        release.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return status, checks
    status, checks = asyncio.run(go())
    assert status == 503 and checks["rag"] == "pending"


def test_the_readiness_request_never_probes(client, monkeypatch, lane):
    """The rule the fix must keep: the probe must never become the outage. The
    unauthenticated route reads the last pass and never reaches the embed
    service or the vector store."""
    touched = []

    class _Tripwire:
        def list_collections(self):
            touched.append("vector store")
            return []
    monkeypatch.setattr(db, "client", _Tripwire())
    monkeypatch.setattr(db, "_embed", lambda *a, **k: touched.append("embed") or [0.0])
    for _ in range(3):
        client.get("/api/health/ready")
    assert touched == []


def test_the_reason_reaches_the_detailed_view_not_the_public_body(
        client, admin_headers, monkeypatch, lane, tmp_path):
    monkeypatch.setattr(db, "client", _Client(error=RuntimeError("segment unreadable")))
    _timer_pass(str(tmp_path))
    r = client.get("/api/health/ready")
    assert "vector_store_unreadable" not in r.text and "segment" not in r.text
    detail = client.get("/api/health/detailed", headers=admin_headers).json()
    assert detail["rag"]["state"] == "error"
    assert detail["rag"]["reason"] == "vector_store_unreadable"


def test_the_startup_hook_wires_the_lane_probe(client):
    """The conftest runs the timer off, so readiness reads "off" in the rest
    of the suite - but the hook must still have passed the probe in."""
    assert sc._rag_wired is True
