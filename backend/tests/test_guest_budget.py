"""The guest wallet backstop, end to end.

check_daily_guest_budget shipped DEFINED AND NEVER CALLED: the function sat in
security.py with no call site anywhere in the repo, so a reader auditing the
guest lane would reasonably conclude total guest spend was capped when nothing
capped it. GUEST_MAX_TURNS bounds one conversation and check_rate_limit bounds
one IP; neither bounds the day, which is what an operator's bill cares about.

These pin the CALL as well as the behaviour, because the failure mode here was
never a wrong answer - it was a guard that existed and reached no caller, which
a behaviour suite passes right over.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import security
from app.routers import chat as chat_mod
from app.routers import system as system_mod


def _mock_stream_events(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "ok"}


def _guest_enabled_cfg(key, default=None):
    """Open the admin half of the double-latched guest gate, leave the rest."""
    if key == "guest_mode_enabled":
        return "true"
    return default


@pytest.fixture(autouse=True)
def _clean_guest_budget():
    # Process-global by design, and the client fixture is session-scoped, so
    # without this each case would start wherever the previous one stopped.
    security._daily_guest_store.clear()
    yield
    security._daily_guest_store.clear()


@pytest.fixture(autouse=True)
def _pin_the_in_memory_branch():
    # REDIS_URL is read once and latched in redis_client, so setting the env
    # var inside a test is inert - the accessor is the only steerable seam.
    # security.py imports it inside the function, so it resolves on
    # redis_client at call time and this patch reaches it.
    with patch("app.redis_client.get_redis", return_value=None):
        yield


# Distinct per caller on purpose. chat_sessions.session_id is UNIQUE across the
# whole table while the meta upsert looks it up owner-scoped, so a guest and a
# signed-in user both landing on the default id make the second one INSERT into
# a key that is already taken. That is a defect in its own right and not this
# control's business - these name their own sessions so they measure the budget.
_GUEST_SESSION = "guest-budget-test"
_AUTH_SESSION = "guest-budget-test-auth"


def _guest_chat(client):
    with patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events):
        return client.post("/api/chat", json={"prompt": "Hi", "model": "test-model",
                                              "session_id": _GUEST_SESSION})


def test_guest_budget_exhausts_and_answers_the_wallet_429(client, monkeypatch):
    # Patch the name on the CHAT module: chat.py binds it at import, so
    # patching runtime_config (where it is defined) would not move what the
    # handler reads.
    monkeypatch.setattr(chat_mod, "DEMO_DAILY_GUEST_LIMIT", 3)

    assert [_guest_chat(client).status_code for _ in range(3)] == [200, 200, 200]

    over = _guest_chat(client)
    assert over.status_code == 429
    # Names the copy, because the turn limit directly above this guard is ALSO
    # a 429 - a bare status assertion would pass against the wrong control.
    assert "high demand" in over.json()["detail"].lower()


def test_authenticated_callers_are_never_budgeted(client, admin_headers, monkeypatch):
    monkeypatch.setattr(chat_mod, "DEMO_DAILY_GUEST_LIMIT", 2)
    with patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_mock_stream_events):
        codes = [client.post("/api/chat",
                             json={"prompt": "Hi", "model": "test-model",
                                   "session_id": _AUTH_SESSION},
                             headers=admin_headers).status_code
                 for _ in range(5)]

    assert codes == [200] * 5, codes
    # The counter must not have moved at all. A budget that charged signed-in
    # users would let the operator's own traffic close the door on visitors.
    assert security._daily_guest_store == {}


def test_limit_zero_leaves_the_guest_lane_open(client, monkeypatch):
    monkeypatch.setattr(chat_mod, "DEMO_DAILY_GUEST_LIMIT", 0)
    assert [_guest_chat(client).status_code for _ in range(6)] == [200] * 6


def test_non_positive_limit_returns_before_counting_anything():
    # The endpoint test above only proves the CALLER-side guard. This proves
    # the function's own early return, so the control stays inert even if a
    # future call site forgets the `> 0` check.
    security.check_daily_guest_budget(0)
    security.check_daily_guest_budget(-1)
    assert security._daily_guest_store == {}


def test_day_rollover_evicts_the_stale_counter():
    # The in-memory branch keeps only today's key. Without the eviction a
    # long-running process accumulates one dead entry per day it survives.
    security._daily_guest_store["19700101"] = 99
    security.check_daily_guest_budget(5)
    assert "19700101" not in security._daily_guest_store
    assert list(security._daily_guest_store.values()) == [1]


def test_the_guard_is_wired_at_the_chat_handler():
    """The defect this file closes was a guard with no caller, and the shape of
    that bug is that everything else stays green. This is the same substring
    the fleet drift checker pins from outside the repo - asserted here so the
    repo carries its own copy of the claim."""
    src = Path(chat_mod.__file__).read_text(encoding="utf-8")
    assert "check_daily_guest_budget(DEMO_DAILY_GUEST_LIMIT)" in src


def test_status_reports_the_configured_budget_not_a_placeholder(client, admin_headers,
                                                              monkeypatch):
    """A fail-open control (0 = inert) is silent when it is off, so the only
    way an operator can tell a configured cap from a forgotten one is a
    positive signal on the status surface. Asserting the VALUE, not just the
    key: a row hardcoded to 0 would satisfy a presence check while telling
    every operator the cap is off."""
    monkeypatch.setattr(system_mod, "DEMO_DAILY_GUEST_LIMIT", 7)
    r = client.get("/api/status", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["guest_daily_limit"] == 7


def test_redis_failure_degrades_to_the_in_process_counter(client, monkeypatch):
    """get_redis latches its client on first use, so a Redis that dies after a
    successful ping keeps handing back a live-looking handle and every guest
    request would raise out of the guard. Wiring an orphaned function is what
    made that reachable, so it is pinned here: the lane must stay up and the
    request must still be counted, just per-process instead of globally."""
    broken = MagicMock()
    broken.incr.side_effect = ConnectionError("redis went away mid-day")

    monkeypatch.setattr(chat_mod, "DEMO_DAILY_GUEST_LIMIT", 5)
    # Nested patch supersedes the autouse in-memory pin for this test only.
    with patch("app.redis_client.get_redis", return_value=broken):
        r = _guest_chat(client)

    assert r.status_code == 200, "a Redis outage must not 500 the guest lane"
    broken.incr.assert_called_once()
    # Degrading must not quietly stop enforcing - the request still counted.
    assert sum(security._daily_guest_store.values()) == 1
