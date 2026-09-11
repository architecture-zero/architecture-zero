"""The identity card's clearance gate, proven at RUNTIME.

The owner's profile (IDENTITY_CARD_PATH) is pinned into the system prompt so
the assistant always knows who it serves. Until 2026-09-11 it rode EVERY chat
turn regardless of the caller's tier, while content of that kind is
Owner-only for retrieval - disclosure to a guest or member was mediated by a
prompt instruction, which is not a gate. The gate now uses the retrieval
classifier's own floor for the restricted department.

A test that asserts the gate's arithmetic (owner >= floor > member) proves
nothing about the prompt a real session assembles. These tests probe at the
finding's own tier: boot the app, mint real sessions at every rung, drive
POST /api/chat through the real handler, and read the prompt that reaches the
provider boundary (the stream_chat_events seam) - both the cache-stable
`system_prompt` kwarg and the system message the model sees.

A sentinel profile is planted so the assertion is exact and never skips (a
test layout has no profile file). Every below-clearance test first proves the
card WAS loadable in-process, so an absence is the gate withholding it, never
a missing file.

The turn log's `chat_tools_attached` receipt carries `identity_card` and
`caller_level` - the live-tier signal for the same gate, asserted here so the
receipt is a tested fact, not a field nobody reads.
"""
import contextlib
from unittest.mock import patch

import pytest

from app.permissions import (GUEST_LEVEL, MEMBER_LEVEL, ADMIN_LEVEL,
                             OWNER_LEVEL)

_SENTINEL = "IDENTITY-CARD-SENTINEL-7f3a"
_CARD_HEADER = "ABOUT THE HUMAN YOU ARE ASSISTING"
_OWNER_PASSWORD = "AdminPass1"  # conftest's claimed first account


@pytest.fixture
def planted_profile(tmp_path, monkeypatch):
    """A profile the card reads, with a marker no real profile carries.

    monkeypatch restores IDENTITY_CARD_PATH and the read-once cache slot at
    teardown, so the next reader sees the real layout, not the sentinel."""
    profile = tmp_path / "owner-profile.md"
    profile.write_text(
        f"# Owner profile\n{_SENTINEL}: the owner's private profile text.\n",
        encoding="utf-8")
    monkeypatch.setattr("app.routers.chat.IDENTITY_CARD_PATH", str(profile))
    monkeypatch.setattr("app.routers.chat._IDENTITY_CARD", None)


def _guest_enabled_cfg(key, default=None):
    if key == "guest_mode_enabled":
        return "true"
    return default


def _login(client, username, password):
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed for {username}: {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _account(client, owner_headers, username, password, role):
    """A real account at `role`, minted through the step-up door (the
    caller's password). Idempotent across the session DB: a re-run finds the
    row already there and the login still proves the rung."""
    client.post("/api/users",
                json={"current_password": _OWNER_PASSWORD, "username": username,
                      "password": password, "role": role},
                headers=owner_headers)
    return _login(client, username, password)


def _turn(client, headers, guest=False):
    """One real chat turn through the handler; returns what reached the
    provider seam and every turn-log line the handler wrote."""
    seen = {"turns": [], "logs": []}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        seen["turns"].append({"messages": messages, "system_prompt": system_prompt})
        yield {"type": "text", "text": "ok"}

    def _log(event, **kw):
        seen["logs"].append((event, kw))

    if guest:
        cfg = contextlib.ExitStack()
        cfg.enter_context(patch("app.routers.chat.guest_chat_available",
                                return_value=True))
        cfg.enter_context(patch("app.routers.chat.get_config",
                                side_effect=_guest_enabled_cfg))
    else:
        cfg = contextlib.nullcontext()
    with patch("app.routers.chat.stream_chat_events", side_effect=_stream), \
         patch("app.routers.chat.log", side_effect=_log), cfg:
        r = client.post("/api/chat",
                        json={"prompt": "who are you talking to?",
                              "model": "test-model", "use_rag": False},
                        headers=headers)
    assert r.status_code == 200, r.text
    assert len(seen["turns"]) == 1, "expected exactly one provider call"
    return seen


def _card_rode(seen):
    """True iff the card reached the provider - on BOTH the system message
    and the cache-stable core, header and body together or not at all."""
    turn = seen["turns"][0]
    system = turn["messages"][0]
    assert system["role"] == "system"
    flags = (_SENTINEL in system["content"], _CARD_HEADER in system["content"],
             _SENTINEL in turn["system_prompt"], _CARD_HEADER in turn["system_prompt"])
    assert len(set(flags)) == 1, f"card partially present: {flags}"
    # Prompt-cache contract: the system message starts with the stable core.
    assert system["content"].startswith(turn["system_prompt"])
    return flags[0]


def _receipt(seen):
    rows = [kw for ev, kw in seen["logs"] if ev == "chat_tools_attached"]
    assert len(rows) == 1, "one attach receipt per turn"
    return rows[0]


def _card_loadable():
    from app.routers.chat import _identity_card
    assert _SENTINEL in _identity_card(), "the planted profile did not load"


def test_owner_turn_carries_the_card(client, admin_headers, planted_profile):
    """The control: with the profile planted, the Owner's turn carries it -
    so the absences below are the gate, not a missing file."""
    seen = _turn(client, admin_headers)
    assert _card_rode(seen) is True
    receipt = _receipt(seen)
    assert receipt["identity_card"] is True
    assert receipt["caller_level"] == OWNER_LEVEL


def test_admin_turn_has_no_card(client, admin_headers, planted_profile):
    """Admin is one rung below the restricted floor: the owner's profile is
    withheld from an administrator's turns too."""
    _card_loadable()
    headers = _account(client, admin_headers, "idcard_admin", "IdCardA1", "admin")
    seen = _turn(client, headers)
    assert _card_rode(seen) is False
    receipt = _receipt(seen)
    assert receipt["identity_card"] is False
    assert receipt["caller_level"] == ADMIN_LEVEL


def test_member_turn_has_no_card(client, admin_headers, planted_profile):
    """Boot with a member account, read the assembled prompt - the card is
    absent."""
    _card_loadable()
    headers = _account(client, admin_headers, "idcard_member", "IdCardM1", "member")
    seen = _turn(client, headers)
    assert _card_rode(seen) is False
    receipt = _receipt(seen)
    assert receipt["identity_card"] is False
    assert receipt["caller_level"] == MEMBER_LEVEL


def test_guest_turn_has_no_card(client, planted_profile):
    """A guest-turn system prompt - the path a public demo surface serves to
    anyone - carries no owner profile."""
    _card_loadable()
    seen = _turn(client, headers=None, guest=True)
    assert _card_rode(seen) is False
    receipt = _receipt(seen)
    assert receipt["identity_card"] is False
    assert receipt["caller_level"] == GUEST_LEVEL


def test_gate_is_the_retrieval_floor():
    """The authority is the retrieval classifier's own floor for the
    restricted department - not a level typed into the chat handler. Owner
    clears it; every other rung sits below it."""
    from app.rag_config import department_min_level
    floor = department_min_level("restricted")
    assert OWNER_LEVEL >= floor
    assert max(ADMIN_LEVEL, MEMBER_LEVEL, GUEST_LEVEL) < floor
