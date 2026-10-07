"""The instance checks itself (2026-10-02; the upgrade rehearsal's leg 5).

The disk and Ollama alerts were raised inside GET /api/health/detailed and
nowhere else, so an unwatched box sent nothing: the 2026-09-29 rehearsal
stopped Ollama, waited five minutes with nobody signed in, and nothing was
sent until one Owner read of the route. These tests pin the timer that runs
the same probes - and the backup heartbeats - with no reader at all.
"""
import asyncio
import inspect

import pytest

from app import self_check


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """The log remembers the last pass's failures; every test starts clean.
    And the timer's own state - what it watches, when it started, what each
    pass found - goes back after each test: the timer tests below start it
    with nothing wired, and the readiness tests read the boot's wiring."""
    monkeypatch.setattr(self_check, "_last_failing", ())
    monkeypatch.setattr(self_check, "_last_logged", 0.0)
    for name in ("_rag_wired", "_ollama_wired", "_redis_wired", "_started_at",
                 "_rag_last", "_ollama_last", "_redis_last"):
        monkeypatch.setattr(self_check, name, getattr(self_check, name))


@pytest.fixture
def fired(monkeypatch):
    calls = []
    monkeypatch.setattr(self_check, "fire_alert",
                        lambda key, title, body: calls.append(key))
    return calls


class _Usage:
    def __init__(self, used, total):
        self.used, self.total, self.free = used, total, total - used


def _down(path, timeout):
    raise ConnectionError("refused")


def _reader(ok):
    """A stand-in for the deployment's heartbeat reader (on the template, the
    function /api/backup-status serves - the wiring tests pin that)."""
    state = ({"ok": True, "age_hours": 1.0} if ok
             else {"ok": False, "age_hours": None, "reason": "status file missing/unreadable"})
    return lambda fname: dict(state)


# -- The probes ----------------------------------------------------------------

def test_disk_over_the_threshold_fires(monkeypatch, fired, tmp_path):
    monkeypatch.setattr(self_check.shutil, "disk_usage", lambda p: _Usage(90, 100))
    out = self_check.probe_disk(str(tmp_path))
    assert out["ok"] is False and out["pct"] == 90.0
    assert fired == ["disk_high"]


def test_disk_under_the_threshold_is_quiet(monkeypatch, fired, tmp_path):
    monkeypatch.setattr(self_check.shutil, "disk_usage", lambda p: _Usage(10, 100))
    assert self_check.probe_disk(str(tmp_path))["ok"] is True
    assert fired == []


def test_ollama_down_fires(fired):
    assert self_check.probe_ollama(_down) == {"name": "ollama", "ok": False,
                                              "latency_ms": None}
    assert fired == ["ollama_down"]


def test_missing_backup_heartbeats_fire(fired):
    states = self_check.probe_backups(_reader(ok=False))
    assert not states["backup"]["ok"] and not states["drill"]["ok"]
    assert sorted(fired) == ["backup_backup", "backup_drill"]


def test_fresh_backup_heartbeats_are_quiet(fired):
    states = self_check.probe_backups(_reader(ok=True))
    assert states["backup"]["ok"] and states["drill"]["ok"]
    assert fired == []


def test_a_deployment_with_no_drill_watches_the_backup_alone(fired):
    states = self_check.probe_backups(_reader(ok=False), ("backup",))
    assert list(states) == ["backup"]
    assert fired == ["backup_backup"]


def test_one_pass_runs_every_probe_and_names_what_failed(monkeypatch, fired, tmp_path):
    logged = []
    monkeypatch.setattr(self_check, "log", lambda event, **kw: logged.append((event, kw)))
    monkeypatch.setattr(self_check.shutil, "disk_usage", lambda p: _Usage(95, 100))
    monkeypatch.setenv("ENABLE_OLLAMA", "true")
    monkeypatch.setattr(self_check, "SELF_CHECK_BACKUP", True)
    self_check.run_self_check(str(tmp_path), _down, _reader(ok=False))
    assert sorted(fired) == ["backup_backup", "backup_drill", "disk_high", "ollama_down"]
    assert ("self_check_failing", {"checks": ["disk", "ollama", "backup", "drill"]}) in logged


def test_what_is_not_passed_is_not_watched(monkeypatch, fired, tmp_path):
    """A surface whose Ollama or backups are watched elsewhere passes None."""
    monkeypatch.setattr(self_check.shutil, "disk_usage", lambda p: _Usage(10, 100))
    monkeypatch.setenv("ENABLE_OLLAMA", "true")
    out = self_check.run_self_check(str(tmp_path), None, None)
    assert "ollama" not in out and "backups" not in out
    assert fired == []


def test_the_switches_turn_probes_off(monkeypatch, fired, tmp_path):
    monkeypatch.setattr(self_check.shutil, "disk_usage", lambda p: _Usage(10, 100))
    monkeypatch.setenv("ENABLE_OLLAMA", "false")
    monkeypatch.setattr(self_check, "SELF_CHECK_BACKUP", False)
    out = self_check.run_self_check(str(tmp_path), _down, _reader(ok=False))
    assert "ollama" not in out and "backups" not in out
    assert fired == []


# -- The log -------------------------------------------------------------------

def test_the_log_says_what_changed_not_every_pass(monkeypatch):
    logged = []
    monkeypatch.setattr(self_check, "log", lambda event, **kw: logged.append((event, kw)))
    self_check._note(["disk"])
    self_check._note(["disk"])          # the same state again: quiet
    self_check._note([])                # recovered
    assert logged == [("self_check_failing", {"checks": ["disk"]}),
                      ("self_check_recovered", {"checks": ["disk"]})]


def test_a_check_that_stays_down_is_repeated_each_cooldown(monkeypatch):
    logged = []
    monkeypatch.setattr(self_check, "log", lambda event, **kw: logged.append((event, kw)))
    self_check._note(["ollama"])
    monkeypatch.setattr(self_check, "_last_logged", 0.0)   # as if a cooldown had passed
    self_check._note(["ollama"])
    assert logged[-1] == ("self_check_failing", {"checks": ["ollama"], "reminder": True})


# -- The timer -----------------------------------------------------------------

def _drive(seconds=0.3):
    async def go():
        task = asyncio.create_task(self_check.self_check_loop(".", None, None))
        await asyncio.sleep(seconds)
        task.cancel()
    asyncio.run(go())


def test_the_timer_runs_the_pass_with_no_reader(monkeypatch):
    """The rehearsal's failure, inverted: nobody reads any route and the pass
    runs anyway, one interval after the loop starts and every interval after."""
    runs = []
    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(self_check, "run_self_check", lambda *a: runs.append(a) or {})
    _drive()
    # The fifth is the retrieval-lane probe (AZ-02): none wired in this call.
    assert len(runs) >= 2 and runs[0] == (".", None, None, ("backup", "drill"), None)


def test_a_crashing_pass_does_not_stop_the_timer(monkeypatch):
    runs = []

    def boom(*a):
        runs.append(1)
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(self_check, "run_self_check", boom)
    _drive()
    assert len(runs) >= 2


def test_an_interval_of_zero_turns_it_off(monkeypatch):
    runs = []
    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(self_check, "run_self_check", lambda *a: runs.append(1))
    asyncio.run(asyncio.wait_for(self_check.self_check_loop(".", None, None), 1))
    assert runs == []


# -- Wiring: the def is not the guard, the call is -----------------------------
# (Above this line the file is the same on every surface; below it, each
# surface pins its own startup call and its own detailed route.)

def test_boot_starts_the_timer_with_what_this_deployment_watches():
    """The heartbeats go through _backup_job_state, the function the public
    /api/backup-status probe serves, so the alarm and the probe cannot
    disagree about what is stale."""
    from app import main
    src = inspect.getsource(main.startup_tasks)
    assert "_DATA_DIR, _ollama_get, _backup_job_state," in src
    assert "rag_probe=lambda: probe_retrieval_lane(RAG_ONLY_MODE)" in src   # AZ-02


def test_the_detailed_route_runs_the_same_probes():
    from app.routers import system
    src = inspect.getsource(system.health_detailed)
    assert "probe_disk(_DATA_DIR)" in src and "probe_ollama(_ollama_get)" in src
