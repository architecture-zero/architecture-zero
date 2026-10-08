"""Older evidence never replaces newer (R-AZ-02, the outside follow-up review,
2026-10-08).

THE GAP. The lane's boot pass runs beside the self-check timer (R-AZ-01's
read: awaiting it in front of the timer delayed every alert of the boot and a
hung probe stopped the timer). Every pass recorded its result when it finished,
unconditionally, stamped with the finish time. So a boot probe that came back
late landed after a newer timer pass and replaced its "error" with the older
"ok": the reviewer's fixture read 503, then 200, with exactly two probe calls
and nothing new proven - and the finish stamp made the old answer read fresh.

Now each pass takes a ticket before it probes: its result is recorded only if
no pass that started later has been, and it is stamped with when it started.
The review's case, both orders of a held pass, the stamp, and Ollama's record
(the Owner's detailed view probes it beside the timer) each fail on the code
before the fix; the pin that a pass which starts after another finished still
replaces it passes on both by design. The fix's security read added three
Infos, each tested last: no boot retry starts within a retry period of the
first regular pass, a dropped failure is logged, and the Owner's view reads
the record once.
"""
import asyncio
import threading
import time

import pytest

from app import self_check as sc


@pytest.fixture
def lane(monkeypatch):
    """The timer started at the 300 s interval with the lane probe wired and no
    pass yet; Ollama and Redis out of readiness's way; alerts recorded instead
    of sent."""
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 300)
    monkeypatch.setattr(sc, "_rag_wired", True, raising=False)
    monkeypatch.setattr(sc, "_ollama_wired", False, raising=False)
    monkeypatch.setattr(sc, "_redis_wired", False, raising=False)
    monkeypatch.setattr(sc, "_started_at", time.time(), raising=False)
    monkeypatch.setattr(sc, "_rag_last", None, raising=False)
    monkeypatch.setattr(sc, "_ollama_last", None, raising=False)
    monkeypatch.setenv("ENABLE_OLLAMA", "false")
    monkeypatch.delenv("REDIS_URL", raising=False)
    # Where the crash-loop check exists, a workstation's test boots read as a loop.
    try:
        import app.boot_history as _boot_history
        monkeypatch.setattr(_boot_history, "crash_loop_state", lambda: {"looping": False})
    except ImportError:
        pass
    fired = []
    monkeypatch.setattr(sc, "fire_alert", lambda key, *a, **k: fired.append(key))
    return fired


def _ready(client):
    r = client.get("/api/health/ready")
    return r.status_code, r.json()["checks"]["rag"]


async def _until(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


def test_the_reviews_case_a_late_boot_pass_cannot_replace_a_newer_failure(client, monkeypatch, lane):
    """The review's fixture: the real loop at a 0.3 s interval. The boot pass
    finds the lane ok and is held; the first timer pass finds it failing, and
    readiness says 503; then the held boot answer comes back. Two passes in
    all - readiness must stay 503, not turn 200 on the older answer."""
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 0.3)
    boot_gate, hold = threading.Event(), threading.Event()
    calls, finished = [], []
    real_probe_rag = sc.probe_rag

    def watched(*a, **k):
        try:
            return real_probe_rag(*a, **k)
        finally:
            finished.append(1)
    monkeypatch.setattr(sc, "probe_rag", watched)

    def probe():
        calls.append(1)
        if len(calls) == 1:          # the boot pass: the lane was up, the answer is late
            boot_gate.wait(10)
            return {"state": "ok"}
        if len(calls) == 2:          # the first timer pass: the lane is down
            return {"state": "error", "reason": "embed_unreachable"}
        hold.wait(10)                # no third answer while the test reads
        return {"state": "error", "reason": "embed_unreachable"}

    async def go():
        task = asyncio.create_task(sc.self_check_loop(".", None, None, rag_probe=probe))
        try:
            await _until(lambda: len(finished) >= 1)
            before = await asyncio.to_thread(_ready, client)
            boot_gate.set()
            await _until(lambda: len(finished) >= 2)
            after = await asyncio.to_thread(_ready, client)
            return before, after, len(finished)
        finally:
            hold.set()
            boot_gate.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    before, after, passes = asyncio.run(go())
    assert before == (503, "error")
    assert passes == 2
    assert after == (503, "error"), "the late boot answer replaced the newer failure"


def _held_older_pass(older_answer, newer_answer):
    """Two lane passes: the older starts first and is held, the newer runs start
    to finish, then the older comes back. What readiness reads afterwards."""
    gate, started = threading.Event(), threading.Event()

    def older_probe():
        started.set()
        gate.wait(10)
        return older_answer

    older = threading.Thread(target=sc.probe_rag, args=(older_probe, False))
    older.start()
    try:
        assert started.wait(10)
        sc.probe_rag(lambda: newer_answer, False)
    finally:
        gate.set()
        older.join(10)
    return sc.rag_readiness()


def test_an_older_ok_coming_back_last_does_not_replace_a_newer_error(lane):
    assert _held_older_pass({"state": "ok"},
                            {"state": "error", "reason": "embed_unreachable"}) == ("error", True)
    assert sc.rag_status()["reason"] == "embed_unreachable"


def test_an_older_error_coming_back_last_does_not_replace_a_newer_ok(lane):
    assert _held_older_pass({"state": "error", "reason": "embed_unreachable"},
                            {"state": "ok"}) == ("ok", False)


def test_a_late_answer_is_as_old_as_its_question(monkeypatch, lane):
    """Stamped when it finished, a slow pass read as fresh as a fast one. Its
    age now counts from when it started: one slower than the bound lands
    stale, and a slow one's lease is shorter by its own duration."""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(sc.time, "time", lambda: clock[0])

    def taking(seconds):
        def probe():
            clock[0] += seconds
            return {"state": "ok"}
        return probe

    sc.probe_rag(taking(700), False)
    assert sc.rag_readiness(now=clock[0]) == ("stale", True)

    start = clock[0]
    sc.probe_rag(taking(100), False)
    assert sc.rag_readiness(now=start + 659) == ("ok", False)
    assert sc.rag_readiness(now=start + 661) == ("stale", True)


def test_ollamas_record_keeps_the_same_order(monkeypatch, lane):
    """The Owner's detailed view reads Ollama beside the timer: an older "ok"
    coming back last must not replace a newer "unreachable"."""
    monkeypatch.setattr(sc, "_ollama_wired", True)
    gate, started = threading.Event(), threading.Event()

    class _Answered:
        status_code = 200

    def older_get(path, timeout=None):
        started.set()
        gate.wait(10)
        return _Answered()

    def newer_get(path, timeout=None):
        raise ConnectionError("connection refused")

    older = threading.Thread(target=sc.probe_ollama, args=(older_get,))
    older.start()
    try:
        assert started.wait(10)
        sc.probe_ollama(newer_get)
    finally:
        gate.set()
        older.join(10)
    assert sc.ollama_readiness() == "unreachable"
    assert lane == ["ollama_down"]


def test_a_pass_that_starts_after_another_finished_still_replaces_it(lane):
    """The pin: ordering must not freeze the record - each later pass replaces
    the one before it, whatever it found."""
    for answer, expected in (({"state": "ok"}, ("ok", False)),
                             ({"state": "error", "reason": "embed_refused"}, ("error", True)),
                             ({"state": "not_required"}, ("not_required", False))):
        sc.probe_rag(lambda: answer, False)
        assert sc.rag_readiness() == expected


# -- The fix's security read (2026-10-08) -------------------------------------

def test_no_boot_retry_starts_within_a_retry_period_of_the_first_regular_pass(client, monkeypatch, lane):
    """A boot retry that started just after the timer's first pass outranked
    it. Retries now stop a retry period short, so every boot pass starts
    before the regular one."""
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 0.5)
    monkeypatch.setattr(sc, "BOOT_RETRY_SECONDS", 0.1)
    starts = []   # (when the pass started, whether it alerts - only regular passes do)
    real_probe_rag = sc.probe_rag

    def watched(rag_probe, alert=True):
        starts.append((time.monotonic(), alert))
        return real_probe_rag(rag_probe, alert)
    monkeypatch.setattr(sc, "probe_rag", watched)

    def probe():
        time.sleep(0.05)
        return {"state": "error", "reason": "embed_unreachable"}

    async def go():
        task = asyncio.create_task(sc.self_check_loop(".", None, None, rag_probe=probe))
        try:
            await _until(lambda: any(alert for _, alert in starts))
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(go())
    boot = [t for t, alert in starts if not alert]
    regular = min(t for t, alert in starts if alert)
    assert len(boot) >= 2, "the failing boot pass was not retried"
    assert max(boot) <= regular - 0.1 + 0.02, [round(t - regular, 3) for t in boot]


def test_a_dropped_failure_is_logged(monkeypatch, lane):
    """A failing result outranked by a later-started pass may have seen what
    that pass missed, so the drop is written down."""
    lines = []
    monkeypatch.setattr(sc, "log", lambda event, **kw: lines.append((event, kw.get("check"), kw.get("reason"))))
    _held_older_pass({"state": "error", "reason": "embed_unreachable"}, {"state": "ok"})
    assert ("self_check_failure_superseded", "rag", "embed_unreachable") in lines


def test_the_owners_view_reads_the_record_once(monkeypatch, lane):
    """A pass landing between two reads mixed two passes into one view: one
    pass's word with another's reason."""
    older = {"state": "error", "reason": "embed_unreachable", "at": time.time()}
    newer = {"state": "ok", "reason": None, "at": time.time()}
    monkeypatch.setattr(sc, "_rag_last", older)
    real_freshness = sc._freshness

    def landing(*a, **k):
        sc._rag_last = newer   # a pass records while the view is being built
        return real_freshness(*a, **k)
    monkeypatch.setattr(sc, "_freshness", landing)
    view = sc.rag_status()
    assert (view["state"], view["reason"]) in (("error", "embed_unreachable"), ("ok", None)), view
