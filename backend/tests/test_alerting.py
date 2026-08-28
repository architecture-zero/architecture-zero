"""Alert delivery - the bound and the dormancy, pinned rather than trusted.

The module's own claims: unconfigured instances stay dormant (no pool, no
threads), delivery workers are capped at two, and the per-key cooldown keeps
the submit queue short. Each claim gets a test here because every one of them
is invisible when it fails - a stray thread pool on an unconfigured instance
or an unbounded queue behind a slow webhook degrades quietly, never loudly.
"""

import pytest

from app import alerting


@pytest.fixture(autouse=True)
def _clean_slate(monkeypatch):
    """Every test starts unconfigured with no pool and no cooldown history.

    The pool is shut down if a test built one, so its non-daemon workers do
    not outlive the test that created them.
    """
    monkeypatch.setattr(alerting, "_WEBHOOK_URL", "")
    monkeypatch.setattr(alerting, "_EMAIL_TO", "")
    monkeypatch.setattr(alerting, "_SMTP_HOST", "")
    monkeypatch.setattr(alerting, "_SMTP_USER", "")
    monkeypatch.setattr(alerting, "_SMTP_PASS", "")
    monkeypatch.setattr(alerting, "_last", {})
    yield
    if alerting._POOL is not None:
        alerting._POOL.shutdown(wait=True)
        alerting._POOL = None


def test_unconfigured_fire_builds_nothing(monkeypatch):
    """Dormancy is a fire-time property, not just an import-time one: with no
    channel configured, fire() must not build the pool, spawn a worker, or
    stamp the cooldown (a stamp now would swallow the first REAL alert after
    the operator configures a channel)."""
    sent = []
    monkeypatch.setattr(alerting, "_send_all", lambda *a: sent.append(a))

    alerting.fire("disk_high", "t", "b")

    assert alerting._POOL is None
    assert alerting._last == {}
    assert sent == []


def test_delivery_pool_is_capped_at_two_workers(monkeypatch):
    """The bound that replaced thread-per-alert. If this cap drifts, delivery
    threads are unbounded again and the fix is a comment."""
    monkeypatch.setattr(alerting, "_WEBHOOK_URL", "http://127.0.0.1:9/hook")
    monkeypatch.setattr(alerting, "_send_all", lambda *a: None)

    alerting.fire("disk_high", "t", "b")

    assert alerting._POOL is not None
    assert alerting._POOL._max_workers == 2


def test_cooldown_dedups_the_same_key(monkeypatch):
    """Two fires of one key inside the window deliver once; a different key
    still delivers. The cooldown is per key, not global."""
    monkeypatch.setattr(alerting, "_WEBHOOK_URL", "http://127.0.0.1:9/hook")
    sent = []
    monkeypatch.setattr(alerting, "_send_all", lambda title, body: sent.append(title))

    alerting.fire("disk_high", "first", "b")
    alerting.fire("disk_high", "suppressed", "b")
    alerting.fire("ollama_down", "other-key", "b")
    alerting._POOL.shutdown(wait=True)
    alerting._POOL = None

    assert sent == ["first", "other-key"]
