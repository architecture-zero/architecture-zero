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


@pytest.fixture(autouse=True)
def _a_fresh_boot(monkeypatch):
    # The module remembers this process's stamp and the word readiness last
    # logged; each test starts where a fresh boot would.
    monkeypatch.setattr(bh, "_stamped", None, raising=False)
    monkeypatch.setattr(bh, "_ready_logged", None, raising=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bh, "LOOP_THRESHOLD", 4)
    monkeypatch.setattr(bh, "WINDOW_SECONDS", 3600)
    monkeypatch.setenv("GIT_SHA", "abc1234")
    return tmp_path


@pytest.fixture
def logged(monkeypatch):
    """Every app.logger line from here on, as (level, event, fields)."""
    from app import logger
    lines = []
    monkeypatch.setattr(logger, "log", lambda event, **kw: lines.append(("info", event, kw)))
    monkeypatch.setattr(logger, "log_error", lambda event, **kw: lines.append(("error", event, kw)))
    return lines


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
    # must not raise - startup depends on it - and says it wrote nothing
    assert bh.record_boot(sha="abc1234", now=1.0) is False


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


def test_a_padded_sha_is_stamped_and_read_the_same(store, monkeypatch):
    """A padded GIT_SHA was stamped raw and compared stripped, so no boot ever
    matched and a real loop read as none (the 2026-10-08 read)."""
    monkeypatch.setenv("GIT_SHA", " abc1234 ")
    for i in range(6):
        bh.record_boot(now=bh.time.time() - i * 60)
    state = bh.crash_loop_state()
    assert state["boots"] == 6 and state["looping"] is True


def test_one_entry_with_a_bad_timestamp_does_not_zero_the_count(store):
    now = bh.time.time()
    _write(store, [{"ts": "not a time", "sha": "abc1234"}]
           + [{"ts": now - i * 60, "sha": "abc1234"} for i in range(6)])
    state = bh.crash_loop_state(now=now)
    assert state["boots"] == 6 and state["looping"] is True


def test_a_check_that_cannot_run_shows_unavailable_and_passes(client, store, lane_proven, monkeypatch):
    """Fail open, never an invented outage - the branch had no test."""
    def _broken(now=None):
        raise RuntimeError("history unreadable")
    monkeypatch.setattr(bh, "crash_loop_state", _broken)
    r = client.get("/api/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["crash_loop"] == "unavailable"


def test_each_boot_says_whether_the_check_is_watching(store, monkeypatch, logged):
    """An operator who expects the check can see it is off: "unwatched"
    otherwise shows only in an anonymous readiness body."""
    import asyncio
    from app import main
    asyncio.run(main._record_boot_on_startup())
    monkeypatch.setenv("GIT_SHA", "unknown")
    asyncio.run(main._record_boot_on_startup())
    assert [(lvl, ev, kw["crash_loop_check"]) for lvl, ev, kw in logged] == [
        ("info", "boot_recorded", "watched"),
        ("info", "boot_recorded", "unwatched - built without GIT_SHA")]


def test_a_boot_whose_stamp_cannot_be_written_says_unrecorded_and_passes(
        client, store, lane_proven, monkeypatch, logged):
    """An unwritable BOOT_HISTORY_DIR read "ok" forever - the process cannot
    count its own restarts, and nothing said so (the 2026-10-08 read). Fail
    open, never silent: readiness passes and says "unrecorded", and the boot's
    line is an error."""
    import asyncio
    from app import main
    monkeypatch.setattr(bh, "DATA_DIR", str(store / "nope" / "\0bad"))
    asyncio.run(main._record_boot_on_startup())
    assert bh.crash_loop_state()["recorded"] is False
    r = client.get("/api/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["crash_loop"] == "unrecorded"
    assert [(lvl, ev) for lvl, ev, _ in logged if ev.startswith("boot_")] == [
        ("error", "boot_unrecorded")]


def test_an_unwritable_dir_without_a_commit_says_both(store, monkeypatch, logged):
    monkeypatch.setenv("GIT_SHA", "unknown")
    monkeypatch.setattr(bh, "DATA_DIR", str(store / "nope" / "\0bad"))
    assert bh.record_boot() is False
    bh.log_boot()
    assert [(lvl, ev, kw["crash_loop_check"]) for lvl, ev, kw in logged] == [
        ("error", "boot_unrecorded", "unwatched - built without GIT_SHA")]
    assert bh.crash_loop_readiness() == ("unwatched", False)


def test_a_loop_the_history_holds_still_fails_when_this_boot_went_unrecorded(store, monkeypatch):
    """"Unrecorded" is about THIS boot's stamp; the boots the history already
    holds are still evidence, and a loop among them still fails readiness."""
    _write(store, [{"ts": bh.time.time() - i * 60, "sha": "abc1234"} for i in range(6)])
    monkeypatch.setattr(bh, "_stamped", False)
    assert bh.crash_loop_readiness() == ("looping (6 boots/3600s)", True)


def test_readiness_logs_the_crash_loop_word_when_it_changes_never_per_request(
        client, store, lane_proven, logged):
    """A looping process logged an ERROR on every anonymous readiness hit,
    unlike the lane's log-on-change - and the access log already has each hit
    (the 2026-10-08 read)."""
    _write(store, [{"ts": bh.time.time() - i * 60, "sha": "abc1234"} for i in range(11)])
    for _ in range(3):
        assert client.get("/api/health/ready").status_code == 503
    _write(store, [])
    for _ in range(2):
        assert client.get("/api/health/ready").status_code == 200
    assert [(lvl, ev, kw.get("word")) for lvl, ev, kw in logged
            if ev.startswith("readiness_crash_loop")] == [
        ("error", "readiness_crash_loop_failing", "looping (11 boots/3600s)"),
        ("info", "readiness_crash_loop", "ok")]


def test_the_owner_view_shows_the_crash_loop_word(client, admin_headers, store, monkeypatch):
    """The check's word was absent from the Owner's detailed view, so an Owner
    saw "unwatched" or "unrecorded" only in an anonymous body (the 2026-10-08
    read). One read of the history: the word and the count agree."""
    _write(store, [{"ts": bh.time.time() - i * 60, "sha": "abc1234"} for i in range(6)])
    loop = client.get("/api/health/detailed", headers=admin_headers).json()["crash_loop"]
    assert loop == {"state": "looping (6 boots/3600s)", "failing": True, "watched": True,
                    "recorded": True, "boots": 6, "threshold": 4, "window_s": 3600,
                    "sha": "abc1234"}
    monkeypatch.setattr(bh, "_stamped", False)
    _write(store, [])
    loop = client.get("/api/health/detailed", headers=admin_headers).json()["crash_loop"]
    assert loop["state"] == "unrecorded" and loop["failing"] is False and loop["recorded"] is False


def test_the_owner_view_of_a_check_that_cannot_run(monkeypatch):
    def _broken(now=None):
        raise RuntimeError("history unreadable")
    monkeypatch.setattr(bh, "crash_loop_state", _broken)
    assert bh.crash_loop_status() == {"state": "unavailable", "failing": False}


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
