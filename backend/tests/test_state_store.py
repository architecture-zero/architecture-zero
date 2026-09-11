"""app/state_store.py - the restart-proof home for the security controls' state.

Until 2026-09-11 the throttles' windows, the MFA challenge counters and burned
jtis, and the daily guest budget were dicts in the one uvicorn process; a
redeploy forgot all of them (the 2026-09-04 readiness audit's "per-process
auth stores" finding). These pin the store's contract and, in
`test_a_restart_forgets_nothing`, the finding's closure: reloading the
security module - which is what a process restart does to module-level
dicts - leaves every count and every burned challenge exactly where it was.
"""
import importlib
import threading
import time

import pytest
from fastapi import HTTPException

from app import security, state_store


@pytest.fixture(autouse=True)
def _clean_store():
    state_store.clear()
    yield
    state_store.clear()


def _advance(monkeypatch, seconds: float):
    base = time.time()
    monkeypatch.setattr(state_store, "_now", lambda: base + seconds)


def test_put_get_and_expiry(monkeypatch):
    state_store.put("k", {"a": 1}, ttl=60)
    assert state_store.get("k") == {"a": 1}
    assert state_store.keys() == ["k"]
    _advance(monkeypatch, 61)
    assert state_store.get("k") is None          # an expired row reads as a miss
    assert state_store.keys() == []              # and is dropped on the way out


def test_bump_window_allows_up_to_the_limit_then_refuses(monkeypatch):
    for i in range(3):
        assert state_store.bump_window("w", 60, 3) == (i, True)
    assert state_store.bump_window("w", 60, 3) == (3, False)
    assert state_store.bump_window("w", 60, 3) == (3, False)   # refusals do not extend the window
    _advance(monkeypatch, 61)
    assert state_store.bump_window("w", 60, 3) == (0, True)    # the window rolled off


def test_bump_counter_keeps_its_first_expiry(monkeypatch):
    assert state_store.bump_counter("c", ttl=100) == 1
    _advance(monkeypatch, 90)
    assert state_store.bump_counter("c", ttl=100) == 2         # the second hit does not push the expiry out
    _advance(monkeypatch, 101)
    assert state_store.bump_counter("c", ttl=100) == 1         # expired at the first hit + ttl, restarted


def test_put_if_absent_is_single_use():
    assert state_store.put_if_absent("jti", {"used": True}, 60) is True
    assert state_store.put_if_absent("jti", {"used": True}, 60) is False


def test_update_edits_in_place_and_keeps_the_expiry(monkeypatch):
    doc = state_store.update("m", lambda d: d.__setitem__("n", d["n"] + 1), 100,
                             default={"n": 0})
    assert doc == {"n": 1}
    _advance(monkeypatch, 90)
    doc = state_store.update("m", lambda d: d.__setitem__("n", d["n"] + 1), 100,
                             default={"n": 0})
    assert doc == {"n": 2}
    _advance(monkeypatch, 101)                                  # first write + 100 has passed
    assert state_store.get("m") is None
    doc = state_store.update("m", lambda d: d.__setitem__("n", d["n"] + 1), 100,
                             default={"n": 0})
    assert doc == {"n": 1}                                      # a fresh document, not the stale one


def test_sweep_drops_only_expired_rows(monkeypatch):
    state_store.put("old", {}, ttl=10)
    state_store.put("new", {}, ttl=1000)
    _advance(monkeypatch, 11)
    assert state_store.sweep() == 1
    assert state_store.keys() == ["new"]


def test_clear_by_prefix_leaves_other_namespaces_alone():
    state_store.put("mfa:a", {}, 60)
    state_store.put("setup:1.2.3.4", {}, 60)
    assert state_store.clear("mfa:") == 1
    assert state_store.keys() == ["setup:1.2.3.4"]


def test_concurrent_bumps_lose_nothing():
    """Eight threads hammer one window; every increment must land. This is the
    optimistic-concurrency loop doing its job on the shared database."""
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(20):
                state_store.bump_window("hot", 3600, 10_000)
        except Exception as e:                       # pragma: no cover - a failure IS the finding
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(state_store.get("hot")["ts"]) == 160


def test_a_restart_forgets_nothing():
    """The finding's closure. Two wrong codes against one challenge, another
    challenge burned, one claim attempt from an IP - then the security module
    is reloaded, which recreates every module-level object exactly as a
    process restart would. The store is the database, so nothing is forgotten."""
    assert security.record_mfa_failure("chal-1") == 1
    assert security.record_mfa_failure("chal-1") == 2
    security.burn_mfa_challenge("chal-2")
    security.check_setup_rate_limit("203.0.113.9")

    reloaded = importlib.reload(security)

    assert state_store.get("mfa:chal-1")["attempts"] == 2
    with pytest.raises(HTTPException) as exc:
        reloaded.check_mfa_challenge("chal-2")
    assert exc.value.status_code == 401             # still burned after the "restart"
    assert reloaded.record_mfa_failure("chal-1") == 3   # the count continued, not restarted
    count, _ = state_store.bump_window("setup:203.0.113.9", reloaded.SETUP_WINDOW,
                                       reloaded.SETUP_MAX_ATTEMPTS)
    assert count == 1                                # the earlier claim attempt still counts


def test_the_throttles_share_one_store_and_expire_with_their_windows(monkeypatch):
    for _ in range(security.SETUP_MAX_ATTEMPTS):
        security.check_setup_rate_limit("198.18.0.1")
    with pytest.raises(HTTPException) as exc:
        security.check_setup_rate_limit("198.18.0.1")
    assert exc.value.status_code == 429
    assert "setup:198.18.0.1" in state_store.keys()
    _advance(monkeypatch, security.SETUP_WINDOW + 1)
    security.check_setup_rate_limit("198.18.0.1")   # the window rolled off; allowed again


def test_mfa_challenge_dies_with_its_token_not_later(monkeypatch):
    security.record_mfa_failure("late")
    _advance(monkeypatch, security.MFA_CHALLENGE_TTL - 10)
    security.record_mfa_failure("late")              # a late attempt does not extend the life
    _advance(monkeypatch, security.MFA_CHALLENGE_TTL + 1)
    assert state_store.get("mfa:late") is None       # gone with the token
    security.check_mfa_challenge("late")             # and a fresh challenge with that jti would start clean
