"""The crash-loop signal: what liveness could not see on 2026-08-20.

97 restarts over 10.5 hours produced zero alerts because a crash-looping process
still serves 200s between deaths. These pin the discriminator that makes the
signal usable - SAME-sha boots, so a busy deploy day cannot masquerade as an
outage - and the fail-open behaviour that keeps it trustworthy.
"""
import json
import os

import pytest

from app import boot_history as bh


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bh, "LOOP_THRESHOLD", 4)
    monkeypatch.setattr(bh, "WINDOW_SECONDS", 3600)
    monkeypatch.setenv("GIT_SHA", "abc1234")
    return tmp_path


def _write(store, entries):
    (store / bh.HISTORY_FILE).write_text(json.dumps(entries))


def test_record_boot_appends_and_survives_a_reread(store):
    bh.record_boot(sha="abc1234", now=1000.0)
    bh.record_boot(sha="abc1234", now=1100.0)
    saved = json.loads((store / bh.HISTORY_FILE).read_text())
    assert [b["ts"] for b in saved] == [1000.0, 1100.0]
    assert {b["sha"] for b in saved} == {"abc1234"}


def test_history_is_trimmed_so_it_cannot_grow_without_bound(store):
    for i in range(bh.KEEP + 25):
        bh.record_boot(sha="abc1234", now=float(i))
    saved = json.loads((store / bh.HISTORY_FILE).read_text())
    assert len(saved) == bh.KEEP
    assert saved[-1]["ts"] == float(bh.KEEP + 24)  # newest kept


def test_a_crash_loop_trips_it(store):
    # the real incident's shape: the same sha, over and over, inside the window
    _write(store, [{"ts": 10_000.0 - i * 330, "sha": "abc1234"} for i in range(11)])
    state = bh.crash_loop_state(now=10_000.0)
    assert state["looping"] is True
    assert state["boots"] == 11


def test_a_busy_deploy_day_does_not(store):
    """Four deploys in ninety minutes happened on 2026-08-22 and is normal.

    Each deploy carries a DIFFERENT sha, so counting same-sha boots scores them
    1 apiece. This is the whole reason the discriminator is sha and not a count.
    """
    _write(store, [
        {"ts": 9_000.0, "sha": "aaaa111"},
        {"ts": 9_600.0, "sha": "bbbb222"},
        {"ts": 10_200.0, "sha": "cccc333"},
        {"ts": 10_800.0, "sha": "abc1234"},
    ])
    state = bh.crash_loop_state(now=10_800.0)
    assert state["looping"] is False
    assert state["boots"] == 1


def test_boots_outside_the_window_are_ignored(store):
    _write(store, [{"ts": 1_000.0 + i, "sha": "abc1234"} for i in range(20)])
    state = bh.crash_loop_state(now=1_000_000.0)  # long past the window
    assert state["looping"] is False
    assert state["boots"] == 0


@pytest.mark.parametrize("content", ["", "not json", "{}", '[{"no_ts": 1}]'])
def test_unreadable_history_fails_open(store, content):
    """A monitoring input that invents outages gets muted, and a muted monitor is
    worth less than no monitor. Garbage in must read as 'quiet', never 'down'."""
    (store / bh.HISTORY_FILE).write_text(content)
    state = bh.crash_loop_state(now=10_000.0)
    assert state["looping"] is False
    assert state["boots"] == 0


def test_missing_history_fails_open(store):
    assert not (store / bh.HISTORY_FILE).exists()
    assert bh.crash_loop_state(now=10_000.0)["looping"] is False


def test_record_boot_never_raises_even_with_an_unusable_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bh, "DATA_DIR", str(tmp_path / "nope" / "\0bad"))
    bh.record_boot(sha="abc1234", now=1.0)  # must not raise - startup depends on it


@pytest.fixture
def lane_proven(monkeypatch):
    # The retrieval lane proven a moment ago, so readiness's status is about the
    # crash-loop check alone (an unproven lane fails readiness since R-AZ-01, and
    # the suite's timer is off - without this the 503 below proves nothing).
    from app import self_check as sc
    monkeypatch.setattr(sc, "SELF_CHECK_INTERVAL_SECONDS", 300)
    monkeypatch.setattr(sc, "_rag_wired", True)
    monkeypatch.setattr(sc, "_rag_last", {"state": "ok", "reason": None, "at": bh.time.time()})


def test_readiness_reports_503_and_leaks_nothing_when_looping(client, store, lane_proven):
    _write(store, [{"ts": bh.time.time() - i * 60, "sha": "abc1234"} for i in range(11)])
    r = client.get("/api/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["checks"]["crash_loop"].startswith("looping")
    # public endpoint: pass/fail only, never interpolated exception text
    assert not any("Traceback" in str(v) or "error: " in str(v) for v in body["checks"].values())


def test_readiness_is_green_when_not_looping(client, store, lane_proven):
    _write(store, [{"ts": bh.time.time(), "sha": "abc1234"}])
    r = client.get("/api/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["crash_loop"] == "ok"


def test_a_build_without_its_commit_is_unwatched_never_looping(client, store, lane_proven, monkeypatch):
    """Built without GIT_SHA, every build stamps "unknown": a stranger's setup
    hour of rebuilds would read as one build crash-looping and fail readiness
    for an hour. With no build to tell apart, the check does not watch."""
    monkeypatch.setenv("GIT_SHA", "unknown")
    _write(store, [{"ts": bh.time.time() - i * 60, "sha": "unknown"} for i in range(11)])
    state = bh.crash_loop_state()
    assert state["looping"] is False and state["watched"] is False
    r = client.get("/api/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["crash_loop"] == "unwatched"
    monkeypatch.delenv("GIT_SHA")
    assert bh.crash_loop_state()["watched"] is False


def test_the_first_startup_hook_stamps_the_boot(store):
    """The def is not the guard, the call is: record_boot runs from a startup
    hook, and from the FIRST one, so a boot that dies later - in the background
    task that runs chroma maintenance - was still counted."""
    import asyncio
    from app import main
    assert main.app.router.on_startup[0] is main._record_boot_on_startup
    asyncio.run(main._record_boot_on_startup())
    saved = json.loads((store / bh.HISTORY_FILE).read_text())
    assert [b["sha"] for b in saved] == ["abc1234"]
