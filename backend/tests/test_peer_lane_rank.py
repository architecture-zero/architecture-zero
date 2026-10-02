"""The peer lane's RANK, its place in the prompt, and when RAG-only refuses
(2026-10-02).

Before: every peer piece over the similarity threshold reached the prompt,
pasted AFTER the question as SUPPLEMENTARY CONTEXT, outside the instruction
that governs the context; and in RAG_ONLY_MODE a question the local corpus held
nothing on was refused before the peers were asked at all. Read on a live
three-peer deployment the same day (six questions, no model call): eight pieces
from each peer, twenty-four over the threshold on every question.

Also here, the two read-path faults filed with it: a result whose document is
gone failed the whole search, and a failed peer request wrote the visitor's
question into the breaker's record and the log.
"""
import logging
import socket
from unittest.mock import patch
from urllib.parse import quote_plus

import pytest
import requests as _requests

import app.peers as peers
from app import peer_boundary

QUESTION = "What is the vendor onboarding turnaround?"
REFUSAL = "I can only answer questions based on the documents in my knowledge base."
LOCAL = [{"text": "Local fact: onboarding takes ten business days.",
          "source": "local-onboarding.md", "score": 0.9}]


def _peer_pieces(n_peers: int = 3, per_peer: int = 8) -> list[dict]:
    """Three peers' worth of pieces, every one over the threshold - the shape
    the live read found."""
    return [{"peer": f"peer{p}", "source": f"doc-{p}-{i}.md", "score": 0.9,
             "text": f"peer piece rank-{p * per_peer + i:02d}"}
            for p in range(n_peers) for i in range(per_peer)]


def _rank_last_first(query, pool, top_k=None, stats=None):
    """A stand-in for the cross-encoder that prefers the LAST pieces sent and
    keeps five, so a piece in the prompt proves the rank chose it - not the
    order it arrived in."""
    return list(reversed(pool))[:5]


def _chat(client, admin_headers, pieces, local=(), rag_only=False, use_peers=True):
    """One chat turn with the peers, local retrieval, the rank and the model
    stubbed. Returns (stream body, the messages the model was sent or None)."""
    captured: list = []
    peer_rows = [{"id": "p1", "name": "peer0", "url": "http://peer.test", "enabled": True}]

    def fake_stream(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        captured.append(msgs)
        yield {"type": "text", "text": "ok"}

    with patch("app.routers.chat.get_peers", return_value=peer_rows), \
         patch("app.routers.chat.query_peer_kb", return_value=list(pieces)), \
         patch("app.rerank.retrieve", return_value=list(local)), \
         patch("app.rerank.rerank", side_effect=_rank_last_first), \
         patch("app.routers.chat.RAG_ONLY_MODE", rag_only), \
         patch("app.routers.chat.stream_chat_events", side_effect=fake_stream):
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": QUESTION, "use_peers": use_peers, "use_rag": True,
            "session_id": "peer-lane-rank-test"})
    assert r.status_code == 200
    return r.text, (captured[-1] if captured else None)


# -- the rank --------------------------------------------------------------------

def test_the_peer_pool_is_ranked_to_top_k(client, admin_headers):
    body, msgs = _chat(client, admin_headers, _peer_pieces())
    user_prompt = msgs[-1]["content"]
    assert user_prompt.count("[EXTERNAL PEER CONTENT") == 5
    # The five the rank chose - the last five sent - and none of the first.
    for kept in ("rank-23", "rank-22", "rank-21", "rank-20", "rank-19"):
        assert kept in user_prompt
    assert "rank-00" not in user_prompt and "rank-18" not in user_prompt


def test_the_rank_sees_every_peer_together(client, admin_headers):
    """One pool across the peers, not a top-k per peer: the stand-in keeps the
    last five sent, which are all the third peer's."""
    _, msgs = _chat(client, admin_headers, _peer_pieces())
    user_prompt = msgs[-1]["content"]
    assert "peer2 / doc-2-7.md" in user_prompt
    assert "peer0 /" not in user_prompt and "peer1 /" not in user_prompt


# -- the place in the prompt -------------------------------------------------------

def test_the_peer_block_stands_before_the_question(client, admin_headers):
    _, msgs = _chat(client, admin_headers, _peer_pieces(1, 2), local=LOCAL)
    user_prompt = msgs[-1]["content"]
    assert (user_prompt.index("CONTEXT:\n")
            < user_prompt.index("SUPPLEMENTARY CONTEXT (from connected AI sources):")
            < user_prompt.index("QUESTION:"))
    assert user_prompt.endswith(f"QUESTION: {QUESTION}")
    # One instruction governs both blocks.
    assert user_prompt.startswith("Use the following context to answer the question.")
    assert user_prompt.count("QUESTION:") == 1


def test_a_turn_with_no_peer_piece_builds_the_prompt_as_before(client, admin_headers):
    """The common case is untouched, byte for byte."""
    from app.rerank import format_context
    _, msgs = _chat(client, admin_headers, [], local=LOCAL, use_peers=False)
    assert msgs[-1]["content"] == (
        "Use the following context to answer the question. "
        "Answer from this context - do not offer to read files or fetch additional information.\n\n"
        f"CONTEXT:\n{format_context(LOCAL)}\n\n"
        f"QUESTION: {QUESTION}")


def test_peers_alone_are_held_to_the_same_instruction(client, admin_headers):
    _, msgs = _chat(client, admin_headers, _peer_pieces(1, 2))
    user_prompt = msgs[-1]["content"]
    assert user_prompt.startswith("Use the following context to answer the question.")
    assert "\nCONTEXT:\n" not in user_prompt          # no local block was found
    assert user_prompt.endswith(f"QUESTION: {QUESTION}")


# -- when RAG-only refuses -----------------------------------------------------------

def test_rag_only_answers_from_a_peer_when_the_local_corpus_holds_nothing(client, admin_headers):
    body, msgs = _chat(client, admin_headers, _peer_pieces(1, 1), rag_only=True)
    assert msgs is not None, "refused before the peer was asked - the old shape"
    assert REFUSAL not in body
    user_prompt = msgs[-1]["content"]
    assert user_prompt.startswith("Answer the question using ONLY the context below.")
    assert "rank-00" in user_prompt


def test_rag_only_still_refuses_when_both_lanes_are_empty(client, admin_headers):
    body, msgs = _chat(client, admin_headers, [], rag_only=True)
    assert msgs is None
    assert REFUSAL in body


def test_rag_only_refuses_when_every_peer_piece_is_under_the_threshold(client, admin_headers):
    low = [dict(p, score=0.01) for p in _peer_pieces(1, 3)]
    body, msgs = _chat(client, admin_headers, low, rag_only=True)
    assert msgs is None
    assert REFUSAL in body


# -- select, the one function the route calls --------------------------------------

def test_select_counts_each_step_and_ranks_only_what_was_admitted():
    good = [{"peer": "a", "source": f"g{i}.md", "score": 0.9, "text": f"good {i}"}
            for i in range(3)]
    sent = ["not a piece", {"peer": "a", "source": "low.md", "score": 0.1, "text": "low"},
            *good]
    seen: list = []

    def rank(query, pool):
        seen.append(list(pool))
        return pool[:2]

    report: dict = {}
    kept = peer_boundary.select("q", sent, 0.4, rank=rank, report=report)
    assert report == {"sent": 5, "admitted": 3, "capped": 0, "scanned": 3, "kept": 2}
    assert [c["source"] for c in seen[0]] == ["g0.md", "g1.md", "g2.md"]
    assert len(kept) == 2


def test_select_never_ranks_an_empty_pool():
    def rank(query, pool):
        raise AssertionError("ranked an empty pool")
    report: dict = {}
    assert peer_boundary.select("q", [{"text": "x", "score": 0.0}], 0.4,
                                rank=rank, report=report) == []
    assert report["kept"] == 0


def test_the_route_calls_select(chat_route_src):
    """The lane is ONE function, so a probe that reads it reads the lane."""
    assert "peer_boundary.select(" in chat_route_src


# -- a result with no document ---------------------------------------------------------

def test_a_result_with_no_document_is_dropped_not_raised():
    from app.database import _hybrid_rank
    out = _hybrid_rank(["alpha text", None, "beta text"], [0.2, 0.3, 0.4],
                       [{"source": "a.md"}, None, {"source": "b.md"}], "alpha", 5)
    assert sorted(r["source"] for r in out) == ["a.md", "b.md"]


def test_a_live_piece_with_no_metadata_reads_as_having_none():
    from app.database import _hybrid_rank
    out = _hybrid_rank(["alpha text"], [0.2], [None], "alpha", 5)
    assert len(out) == 1 and out[0]["source"] == "unknown"


def test_only_ghosts_is_an_empty_result():
    from app.database import _hybrid_rank
    assert _hybrid_rank([None, ""], [0.2, 0.3], [None, None], "alpha", 5) == []


# -- the question stays out of the failure record and the log ---------------------------

SECRET = "my appointment with the oncologist on tuesday"
_PEER = {"id": "lane-peer", "name": "Lane Peer", "url": "http://peer.invalid:9"}


@pytest.fixture
def _peer_resolves(monkeypatch):
    monkeypatch.setattr(peers.socket, "getaddrinfo",
                        lambda host, port, *a, **k: [
                            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    peers._save_peer_health(_PEER["id"], {})


def _no_secret(health: dict, caplog) -> None:
    words = ("oncologist", "tuesday", quote_plus(SECRET))
    last = health.get("last_error") or ""
    assert not any(w in last for w in words), last
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert not any(w in logged for w in words), logged


def test_a_failed_connection_records_its_kind_not_the_question(monkeypatch, caplog, _peer_resolves):
    def fail(*a, **k):
        # What urllib3 actually writes: the request line, query string and all.
        raise _requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='peer.invalid', port=9): Max retries exceeded with "
            f"url: /api/query-kb?q={quote_plus(SECRET)}&n=8 (Caused by "
            "NewConnectionError('Connection refused'))")
    monkeypatch.setattr(peers, "_pinned_request", fail)
    caplog.set_level(logging.DEBUG)
    assert peers.query_peer_kb(_PEER, SECRET) == []
    health = peers.get_peer_health(_PEER["id"])
    assert health["last_error"] == "connection error: ConnectionError"
    _no_secret(health, caplog)


def test_an_http_error_records_the_status_not_the_body(monkeypatch, caplog, _peer_resolves):
    class _Refused:
        status_code = 422
        text = ('{"detail":[{"loc":["query","q"],"msg":"too long",'
                f'"input":"{SECRET}"}}]}}')

        def raise_for_status(self):
            raise _requests.exceptions.HTTPError(
                f"422 Client Error for url: http://peer.invalid:9/api/query-kb?q={quote_plus(SECRET)}",
                response=self)

    monkeypatch.setattr(peers, "_pinned_request", lambda *a, **k: _Refused())
    monkeypatch.setattr(peers, "_release", lambda resp: None)
    caplog.set_level(logging.DEBUG)
    assert peers.query_peer_kb(_PEER, SECRET) == []
    health = peers.get_peer_health(_PEER["id"])
    assert health["last_error"] == "HTTP 422"
    _no_secret(health, caplog)


def test_an_unexpected_error_is_named_by_its_kind(monkeypatch, caplog, _peer_resolves):
    def fail(*a, **k):
        raise _requests.exceptions.ChunkedEncodingError(
            f"broken while reading /api/query-kb?q={quote_plus(SECRET)}")
    monkeypatch.setattr(peers, "_pinned_request", fail)
    caplog.set_level(logging.DEBUG)
    assert peers.query_peer_kb(_PEER, SECRET) == []
    health = peers.get_peer_health(_PEER["id"])
    assert health["last_error"] == "ChunkedEncodingError"
    _no_secret(health, caplog)


def test_a_successful_query_does_not_log_the_question(monkeypatch, caplog, _peer_resolves):
    class _OK:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"text": "t", "source": "s.md"}]}

    monkeypatch.setattr(peers, "_pinned_request", lambda *a, **k: _OK())
    monkeypatch.setattr(peers, "_release", lambda resp: None)
    caplog.set_level(logging.DEBUG)
    assert len(peers.query_peer_kb(_PEER, SECRET)) == 1
    _no_secret(peers.get_peer_health(_PEER["id"]), caplog)


# -- the bounds the security read asked for (2026-10-02) ---------------------------------

def test_select_bounds_the_pool_before_the_scan_and_the_rank():
    many = [{"peer": "a", "source": f"m{i}.md", "score": 0.9, "text": f"piece {i}"}
            for i in range(peer_boundary.POOL_MAX + 36)]
    seen: list = []

    def rank(query, pool):
        seen.append(len(pool))
        return pool[:5]

    report: dict = {}
    peer_boundary.select("q", many, 0.4, rank=rank, report=report)
    assert seen == [peer_boundary.POOL_MAX]
    assert report["capped"] == 36 and report["admitted"] == peer_boundary.POOL_MAX


def test_a_peer_that_ignores_n_is_kept_to_what_was_asked(monkeypatch, _peer_resolves):
    class _Flood:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"text": f"t{i}", "source": "s.md"} for i in range(500)]}

    monkeypatch.setattr(peers, "_pinned_request", lambda *a, **k: _Flood())
    monkeypatch.setattr(peers, "_release", lambda resp: None)
    assert len(peers.query_peer_kb(_PEER, "q", n_results=8)) == 8


def test_a_piece_that_is_not_an_object_does_not_cost_the_peer_its_answer(monkeypatch, _peer_resolves):
    class _Mixed:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": ["not a piece", {"text": "t", "source": "s.md"}]}

    monkeypatch.setattr(peers, "_pinned_request", lambda *a, **k: _Mixed())
    monkeypatch.setattr(peers, "_release", lambda resp: None)
    out = peers.query_peer_kb(_PEER, "q")
    assert len(out) == 2 and out[1]["peer"] == _PEER["name"]


def test_results_that_are_not_a_list_record_their_own_words(monkeypatch, _peer_resolves):
    class _Odd:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": {"text": "t"}}

    monkeypatch.setattr(peers, "_pinned_request", lambda *a, **k: _Odd())
    monkeypatch.setattr(peers, "_release", lambda resp: None)
    assert peers.query_peer_kb(_PEER, "q") == []
    assert peers.get_peer_health(_PEER["id"])["last_error"] == (
        "peer answered with results that are not a list")


def test_a_peer_cannot_choose_its_own_tier():
    """A peer's JSON is not validated on the way in: a piece that calls itself
    curated, or a generated system record, still wears the EXTERNAL label
    (the security read of the peer-lane port, 2026-10-02)."""
    from app.rerank import format_peer_context
    out = format_peer_context([{"peer": "p", "source": "s.md", "text": "x",
                                "trust": "curated", "auto_generated": True}])
    assert "[EXTERNAL PEER CONTENT" in out
    assert "LIVE SYSTEM RECORD" not in out
    assert "\n[s.md]\n" not in out
