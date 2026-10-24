"""Degraded-vs-no-match: an index outage must not be spoken as missing knowledge.

query_similar logged a failed collection and moved on, right under a comment
saying not to hide infrastructure failures behind an empty result. When every
collection failed, the caller got [] and could not tell "the corpus has nothing
on this" from "the index is down" - and in RAG_ONLY_MODE the chat turned [] into
its refusal, so an OUTAGE reached the user as a confident "I don't have relevant
information to answer that question".

[] and "down" stay the same value to every caller until something asserts the
difference, so these tests assert it at each place it has to hold: the
retrieval layer, the chat answer, the peer serve route, the eval row and the
offline retrieval script. The collections below are the shape of a live but
sick index - count() answers and the search fails - which is what corruption
looks like in practice; nothing here stubs retrieve() itself, so each test runs
the real chain down to query_similar.
"""
import json
import os
import re
import sys
import types
from unittest.mock import patch

import pytest

import app.database as database
from app.logger import log_error as _real_log_error

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

QUESTION = "What is the vendor onboarding turnaround?"
REFUSAL = "I don't have relevant information"
OUTAGE = "I could not reach my knowledge base"
PEER_TEXT = "Peer fact: onboarding takes ten business days."


class _Sick:
    """count() works and the search fails: a live-but-sick index."""

    def __init__(self, name):
        self.name = name

    def count(self):
        return 42

    def query(self, *a, **kw):
        raise RuntimeError("hnsw segment unreadable")

    def get(self, *a, **kw):
        raise RuntimeError("hnsw segment unreadable")


class _Fine:
    def __init__(self, name):
        self.name = name

    def count(self):
        return 1

    def query(self, *a, **kw):
        return {"ids": [["a"]], "documents": [[f"hello from {self.name}"]],
                "distances": [[0.1]], "metadatas": [[{"source": f"{self.name}.md"}]]}

    def get(self, ids=None, include=None, where=None):
        return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}


class _Empty(_Fine):
    def count(self):
        return 0


@pytest.fixture(autouse=True)
def _clean_lexical_cache():
    database._LEX_INDEX.clear()
    yield
    database._LEX_INDEX.clear()


def _collections(monkeypatch, global_col, departments=None):
    """Both collection seams (test_bm25_leg's _use says why): the global
    collection comes from _get_collection, a department's from
    _existing_collection, which answers None for a department not listed."""
    departments = departments or {}
    monkeypatch.setattr(database, "_get_collection", lambda department=None: global_col)
    monkeypatch.setattr(database, "_existing_collection",
                        lambda department=None: departments.get(department))


# -- the retrieval layer -------------------------------------------------------

def test_total_failure_raises_rather_than_returning_empty(monkeypatch):
    _collections(monkeypatch, _Sick("knowledge_base"))
    with pytest.raises(database.RetrievalUnavailable) as exc:
        database.query_similar("anything")
    # The message names what failed - an operator reading the log needs it.
    assert "knowledge_base" in str(exc.value)


def test_every_queried_collection_failing_is_a_total_failure(monkeypatch):
    _collections(monkeypatch, _Sick("knowledge_base"), {"hr": _Sick("kb_hr")})
    with pytest.raises(database.RetrievalUnavailable) as exc:
        database.query_similar("anything", department="hr")
    assert "knowledge_base" in str(exc.value) and "kb_hr" in str(exc.value)


def test_a_failure_beside_an_empty_collection_is_an_outage(monkeypatch):
    """A failed department beside an EMPTY global (the empty one is skipped,
    so it answers nothing) is an outage, not a partial failure: nothing that
    answered returned a candidate, and [] would be narrated as missing
    knowledge."""
    _collections(monkeypatch, _Empty("knowledge_base"), {"hr": _Sick("kb_hr")})
    with pytest.raises(database.RetrievalUnavailable) as exc:
        database.query_similar("anything", department="hr")
    assert "kb_hr" in str(exc.value)


def test_a_query_that_cannot_be_embedded_is_an_outage(monkeypatch):
    """No vector, nothing searched: a total outage, said as one - not the
    embed service's raw error, and never []."""
    import requests

    def _down(*a, **kw):
        raise requests.ConnectionError("embed service unreachable")

    _collections(monkeypatch, _Fine("knowledge_base"))
    monkeypatch.setattr(database, "_embed", _down)
    with pytest.raises(database.RetrievalUnavailable) as exc:
        database.query_similar("anything")
    assert "could not be embedded" in str(exc.value)
    assert isinstance(exc.value.__cause__, requests.ConnectionError)


@pytest.mark.parametrize("global_ok", [True, False],
                         ids=["department-down", "global-down"])
def test_partial_failure_still_returns_the_surviving_candidates(monkeypatch, global_ok):
    """A partial outage degrades QUIETLY on purpose: the healthy collection
    returned real candidates, so the answer is narrower, not wrong. Raising
    here would turn a survivable degradation into a refusal."""
    if global_ok:
        _collections(monkeypatch, _Fine("knowledge_base"), {"hr": _Sick("kb_hr")})
        survivor = "hello from knowledge_base"
    else:
        _collections(monkeypatch, _Sick("knowledge_base"), {"hr": _Fine("kb_hr")})
        survivor = "hello from kb_hr"
    out = database.query_similar("anything", department="hr")
    assert [r["text"] for r in out] == [survivor]


def test_empty_corpus_is_not_an_outage(monkeypatch):
    """CONTROL: zero documents is a legitimate empty, not a failure. The whole
    point is that the two stop being the same value, so conflating them the
    other way would be just as wrong."""
    _collections(monkeypatch, _Empty("knowledge_base"))
    assert database.query_similar("anything") == []


def test_healthy_retrieval_does_not_raise(monkeypatch):
    """CONTROL."""
    _collections(monkeypatch, _Fine("knowledge_base"))
    assert [r["text"] for r in database.query_similar("anything")] == [
        "hello from knowledge_base"]


# -- the chat answer -----------------------------------------------------------

def _tokens(body: str) -> str:
    out = []
    for line in body.splitlines():
        if line.startswith("data: {"):
            event = json.loads(line[len("data: "):])
            if "token" in event:
                out.append(event["token"])
    return "".join(out)


def _chat(client, admin_headers, monkeypatch, *, session, rag_only, peer_pieces=None):
    """One chat turn against a sick index. peer_pieces None = the peers are not
    asked; a list = one enabled peer that sends it. Returns (stream body, the
    messages the model was sent or None, the log_error calls)."""
    _collections(monkeypatch, _Sick("knowledge_base"))
    captured: list = []

    def fake_stream(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        captured.append(msgs)
        yield {"type": "text", "text": "answered"}

    peer_rows = [{"id": "p1", "name": "peer0", "url": "http://peer.test", "enabled": True}]
    with patch("app.routers.chat.get_peers", return_value=peer_rows), \
         patch("app.routers.chat.query_peer_kb", return_value=list(peer_pieces or [])), \
         patch("app.rerank.rerank",
               side_effect=lambda query, pool, top_k=None, stats=None: list(pool)[:5]), \
         patch("app.routers.chat.RAG_ONLY_MODE", rag_only), \
         patch("app.routers.chat.stream_chat_events", side_effect=fake_stream), \
         patch("app.routers.chat.log_error", wraps=_real_log_error) as errors:
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": QUESTION, "model": "test-model", "use_rag": True,
            "use_peers": peer_pieces is not None, "session_id": session})
    assert r.status_code == 200, r.text
    return r.text, (captured[-1] if captured else None), errors.call_args_list


def _stored(client, headers, session):
    return client.get(f"/api/history/{session}", headers=headers).json()["messages"]


def test_rag_only_speaks_the_outage_not_the_refusal(client, admin_headers, monkeypatch):
    """The bug as it was lived: RAG-only, the index down, the peers asked and
    holding nothing. The turn says the knowledge base could not be read, with
    a reference id that is the one the log carries - and it is a stored,
    finished answer (no error event), so the client's row count stays true."""
    from app.metrics import get_snapshot
    before = get_snapshot().get("retrieval_unavailable_total", 0)
    session = "retrieval-down-rag-only"
    body, msgs, errors = _chat(client, admin_headers, monkeypatch, session=session,
                               rag_only=True, peer_pieces=[])
    said = _tokens(body)
    assert OUTAGE in said
    assert REFUSAL not in said
    assert msgs is None, "no model runs on an outage turn"
    assert "[DONE]" in body and '"error"' not in body
    ref = re.search(r"Reference: ([0-9a-f]{12})", said)
    assert ref, said
    logged = [c for c in errors if c.args and c.args[0] == "retrieval_unavailable"]
    assert [c.kwargs.get("error_id") for c in logged] == [ref.group(1)]
    assert get_snapshot().get("retrieval_unavailable_total", 0) == before + 1
    rows = _stored(client, admin_headers, session)
    assert [m["role"] for m in rows] == ["user", "assistant"]
    assert rows[-1]["content"] == said


def test_the_outage_is_spoken_outside_rag_only_mode_too(client, admin_headers, monkeypatch):
    """With no context the grounding rules have the model say the answer is
    not on record - the same false claim about the corpus, in the model's own
    words. So the outage is said whatever the mode."""
    body, msgs, _ = _chat(client, admin_headers, monkeypatch,
                          session="retrieval-down-default-mode", rag_only=False)
    assert OUTAGE in _tokens(body)
    assert msgs is None


def test_a_turn_a_peer_answered_still_answers(client, admin_headers, monkeypatch):
    """CONTROL - the partial-failure rule one level up: the local index is down
    but a peer's piece made it through the lane, so the turn answers from it,
    narrower rather than wrong - never the outage, never the refusal."""
    piece = {"peer": "peer0", "source": "peer-doc.md", "score": 0.9, "text": PEER_TEXT}
    body, msgs, _ = _chat(client, admin_headers, monkeypatch,
                          session="retrieval-down-peer-answered", rag_only=True,
                          peer_pieces=[piece])
    said = _tokens(body)
    assert msgs is not None, "the model must answer from the peer's piece"
    assert PEER_TEXT in msgs[-1]["content"]
    assert said == "answered"
    assert OUTAGE not in body and REFUSAL not in body


# -- the peer serve route --------------------------------------------------------

def test_the_peer_serve_route_answers_unavailable_when_the_index_is_down(monkeypatch):
    """Served as 200 with no results, a down index read to the asking instance
    as "nothing relevant here" and was recorded there as a healthy answer. A
    503 is recorded as this peer failing (its breaker learns it). The detail
    names nothing: collection names are department names, which a peer's
    scope may not cover."""
    from fastapi import HTTPException
    from app.routers.chat import query_kb_for_peer
    _collections(monkeypatch, _Sick("knowledge_base"))
    req = types.SimpleNamespace(state=types.SimpleNamespace(peer_scope="public"))
    with patch("app.routers.chat.list_departments", return_value=["general"]):
        with pytest.raises(HTTPException) as exc:
            query_kb_for_peer(req, q="anything", n=8)
    assert exc.value.status_code == 503
    assert "knowledge_base" not in str(exc.value.detail)


# -- the eval row ------------------------------------------------------------------

def _eval_row(monkeypatch, run_id, *, retrieval_only, use_peers=False):
    """One question through the real eval job against a sick index, with a peer
    that holds the expected source. Returns (the row, the run's state, how many
    times the writer and the peer were called)."""
    import app.eval_runner as runner
    from app.db import get_session
    from app.models import EvalResult

    _collections(monkeypatch, _Sick("knowledge_base"))
    calls = {"writer": 0, "peer": 0}
    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(runner, "supports_tools", lambda model="": False)

    def _writer(*a, **k):
        calls["writer"] += 1
        yield "an answer written without the corpus"

    def _peer(peer, query, n_results=8, timeout=10):
        calls["peer"] += 1
        return [{"source": "docs/held.md", "text": "The answer is held here.",
                 "score": 0.91, "peer": "engineering"}]

    monkeypatch.setattr(runner, "stream_chat", _writer)
    monkeypatch.setattr("app.eval_judge.judge_answer", lambda *a, **k: (1, "ok"))
    monkeypatch.setattr("app.eval_judge.judge_faithfulness", lambda *a, **k: (1, "ok"))
    monkeypatch.setattr("app.eval_judge.judge_freshness", lambda *a, **k: (1, "ok"))
    monkeypatch.setattr("app.peers.get_peers", lambda: [
        {"id": "peer-1", "name": "engineering", "url": "http://peer", "enabled": True}])
    monkeypatch.setattr("app.peers.query_peer_kb", _peer)
    monkeypatch.setattr("app.peer_boundary.select",
                        lambda query, pieces, threshold, rank, on_event=None, report=None:
                        list(pieces))
    questions = [{"id": 9701, "question": "Where is the answer held?", "category": "general",
                  "expected_source": "docs/held.md", "notes": "Must name the file.",
                  "as_level": None}]
    try:
        runner._run_eval_job(run_id, "2026-01-01T00:00:00", questions, model="m",
                             use_rag=True, n_results=5, retrieval_only=retrieval_only,
                             use_peers=use_peers)
        state = dict(runner._eval_runs.get(run_id, {}))
        with get_session() as db:
            r = db.query(EvalResult).filter(EvalResult.run_id == run_id).one()
            row = {"response": r.response or "", "hit": r.retrieval_hit,
                   "rank": r.retrieval_rank, "score": r.score}
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    return row, state, calls


def test_an_eval_row_records_a_retrieval_outage_as_an_error(monkeypatch):
    """The row is an ERRORED row - counted as errored on the runs list and
    auto-failed like any answer the user never got - and it is left out of
    recall: hit 0 would put the question on the Gaps list, sending the
    operator to tune retrieval for an index that was down. No answer is
    written (the chat would not have written one), and the run goes on."""
    row, state, calls = _eval_row(monkeypatch, "retrieval-down-eval-answer",
                                  retrieval_only=False)
    assert row["response"].startswith("[ERROR: retrieval unavailable"), row
    assert row["hit"] is None and row["rank"] is None
    assert row["score"] == 0
    assert calls["writer"] == 0
    assert state.get("complete") and not state.get("failed")


def test_a_local_outage_is_an_errored_row_even_when_a_peer_holds_the_source(monkeypatch):
    """A measurement taken with the local index down is not a measurement of
    the system: the row says outage rather than scoring the peers alone."""
    row, _, calls = _eval_row(monkeypatch, "retrieval-down-eval-peers",
                              retrieval_only=True, use_peers=True)
    assert row["response"].startswith("[ERROR: retrieval unavailable"), row
    assert row["hit"] is None
    assert calls["peer"] == 0


# -- the offline retrieval script ----------------------------------------------------

def test_the_retrieval_script_aborts_a_pass_the_index_cannot_serve(monkeypatch, capsys):
    """A recall pass with the index down measures the outage, not retrieval:
    the script aborts the pass the way it aborts a mislabelled arm (returns
    None, stamps nothing) and puts the reranker hook back."""
    import app.rerank as rr
    import eval_retrieval as ev
    _collections(monkeypatch, _Sick("knowledge_base"))
    monkeypatch.setattr(rr, "status", lambda: {"enabled": False, "loaded": False,
                                               "model": "none", "error": None})
    monkeypatch.setattr("time.sleep", lambda s: None)
    real_rerank = rr.rerank
    q = types.SimpleNamespace(id=1, question="Where is the answer held?",
                              expected_source="docs/held.md", category="general",
                              setup_turns=None)
    assert ev.run_cohort([q], top_k=5, misses_only=False) is None
    assert rr.rerank is real_rerank
    assert "ABORT: retrieval unavailable" in capsys.readouterr().out
