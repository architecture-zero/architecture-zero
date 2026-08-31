"""Clearance at the Eco Mode federation seam - both halves of it.

Every other retrieval surface gates on department_min_level(). Federation did
not, and nothing here noticed. The peer lane looked well tested - an injection
scan (test_peer_boundary_scan.py), a circuit breaker (test_peers_breaker.py),
an SSRF guard (test_ssrf_peer_urls.py) - and not one of those asked how much an
authorized key was allowed to see. Two independent holes met in the middle:

  SERVE - `query_kb_for_peer` mapped scope 'all' to "every department except
  general" and called query_similar() directly. query_similar takes no
  clearance argument (the gate lives one layer up in rerank.retrieve), so a
  peer key handed an off-box caller `restricted`, `history`, and every
  UNLISTED department - the ones DEPARTMENT_DEFAULT_MIN_LEVEL fails closed to
  Owner precisely so they stay private.

  CONSUME - peer chunks bypass retrieve() entirely, so a caller of any local
  clearance received whatever the peer sent.

The fix does NOT ship the caller's level to the peer: a clearance asserted
over the wire is one instance vouching for its own user, which the peer has no
reason to believe. Each side owns one half against the same ladder, and these
tests pin each half separately - a regression in either is a leak on its own.
"""
import types
from unittest.mock import patch, MagicMock

import pytest
from fastapi import HTTPException

from app.permissions import GUEST_LEVEL, MEMBER_LEVEL, ADMIN_LEVEL, OWNER_LEVEL
from app.routers.chat import query_kb_for_peer


# `engineering` is a department the operator deliberately shared at Admin.
# `secret-project` is the dangerous case: a collection that simply exists and
# was never added to DEPARTMENT_MIN_LEVEL, so it fails closed to Owner.
ALL_DEPARTMENTS = ["general", "engineering", "restricted", "history", "secret-project"]
LEVELS = {"general": GUEST_LEVEL, "engineering": ADMIN_LEVEL,
          "restricted": OWNER_LEVEL, "history": OWNER_LEVEL}


def _req(scope):
    return types.SimpleNamespace(state=types.SimpleNamespace(peer_scope=scope))


def _served_departments(scope):
    """Run the serve route and return the department list it actually handed
    query_similar - the real question, since that call is the disclosure."""
    seen = {}

    def fake_query_similar(q, n_results=5, department=None):
        seen["departments"] = department
        return []

    with patch("app.routers.chat.list_departments", return_value=ALL_DEPARTMENTS), patch("app.routers.chat.query_similar", side_effect=fake_query_similar), patch.dict("app.rag_config.DEPARTMENT_MIN_LEVEL", LEVELS, clear=True):
        query_kb_for_peer(_req(scope), q="anything", n=8)
    return seen["departments"]


# ── SERVE side: what may leave ───────────────────────────────────────────────

def test_all_scope_never_serves_owner_only_departments():
    """The regression that motivated the file. 'all' is the scope an operator
    picks when they mean "federate with a peer I trust" - it must not be the
    scope that ships their internal docs."""
    served = _served_departments("all")
    assert "restricted" not in served
    assert "history" not in served
    # Shared at Admin on purpose, so 'all' still does its job.
    assert "engineering" in served


def test_unlisted_department_fails_closed_against_a_peer_key():
    """A collection nobody classified is Owner-only everywhere else in the
    system. The peer seam must not be the exception - this is the case an
    operator never thinks about, because they never named the department."""
    assert "secret-project" not in _served_departments("all")
    assert "secret-project" in _served_departments("owner")


def test_public_scope_serves_the_global_collection_only():
    # None, not [] - query_similar always queries global, and a department
    # list of [] would be a different (and wrong) request shape.
    assert _served_departments("public") is None


def test_owner_scope_serves_internal_departments_when_asked_for_by_name():
    """The capability is preserved, not removed - it just cannot be reached by
    picking the friendly-sounding word."""
    served = _served_departments("owner")
    assert "restricted" in served
    assert "history" in served


@pytest.mark.parametrize("scope", [None, "", "everything", "ALL", "admin", "0"])
def test_unrecognized_scope_grants_nothing(scope):
    with pytest.raises(HTTPException) as exc:
        _served_departments(scope)
    assert exc.value.status_code == 403


def test_scope_ladder_is_monotonic():
    """Higher rung serves a superset. If this ever fails, the mapping has
    developed a special case and the ladder no longer means what it says."""
    public = set(_served_departments("public") or [])
    every = set(_served_departments("all"))
    owner = set(_served_departments("owner"))
    assert public <= every <= owner


# ── CONSUME side: who may receive ────────────────────────────────────────────

def _peer_patches(chunks):
    peers = [{"id": "p1", "name": "testpeer", "url": "http://peer.test", "enabled": True}]
    stamped = [{"peer": "testpeer", "source": "peer-doc.md", "score": 0.9, "text": t}
               for t in chunks]
    return (patch("app.routers.chat.get_peers", return_value=peers),
            patch("app.routers.chat.query_peer_kb", MagicMock(return_value=stamped)))


def _chat(client, headers, captured):
    def fake_stream(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        captured.append(msgs)
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.stream_chat_events", side_effect=fake_stream):
        r = client.post("/api/chat", headers=headers, json={
            "prompt": "What is the vendor onboarding turnaround?",
            "use_peers": True, "session_id": "peer-clearance-test"})
        assert r.status_code == 200
        return r.text


PEER_TEXT = "Standard turnaround for vendor onboarding is 10 business days."


def test_caller_below_the_floor_never_reaches_a_peer(client, admin_headers):
    """Refused BEFORE the network call, not filtered after it. A caller under
    the floor must not cause an outbound query at all - otherwise the peer's
    corpus has already been read on their behalf."""
    captured: list = []
    p_get, p_query = _peer_patches([PEER_TEXT])
    # Floor above Owner: nobody clears it, so the fixture's Owner is 'below'
    # without needing a second user account to prove the rung is read.
    with patch("app.routers.chat.PEER_CONSUME_MIN_LEVEL", OWNER_LEVEL + 1), p_get, p_query:
        body = _chat(client, admin_headers, captured)
        p_query.new.assert_not_called()
    user_prompt = captured[-1][-1]["content"]
    assert PEER_TEXT not in user_prompt
    assert "SUPPLEMENTARY CONTEXT" not in user_prompt
    assert "peers_used" not in body


def test_caller_at_the_floor_still_gets_peer_content(client, admin_headers):
    """The other half of the gate: it bounds the feature, it does not break
    it. Without this, deleting the peer lane outright would pass the suite."""
    captured: list = []
    p_get, p_query = _peer_patches([PEER_TEXT])
    with patch("app.routers.chat.PEER_CONSUME_MIN_LEVEL", GUEST_LEVEL), p_get, p_query:
        body = _chat(client, admin_headers, captured)
        p_query.new.assert_called()
    user_prompt = captured[-1][-1]["content"]
    assert PEER_TEXT in user_prompt
    assert "testpeer" in body


def test_default_floor_shuts_guests_out_of_federation():
    """The shipped default is the one most deployments run. Pinned as a value
    so lowering it is a deliberate edit with a failing test attached."""
    from app.routers import chat
    assert chat.PEER_CONSUME_MIN_LEVEL == MEMBER_LEVEL
    assert chat.PEER_CONSUME_MIN_LEVEL > GUEST_LEVEL
