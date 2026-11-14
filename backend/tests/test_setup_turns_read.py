"""A question's stored setup_turns is read one way, whatever it holds.

Only a JSON list is a history. A stored value that parses to anything else -
a number, a string, true, an object - is an empty history, and the question
is asked as a single turn: what retrieval already made of a string, whose
characters hold no user turn. Read as it was, a number or true reached the
resolver's walk over the turns and raised there, ending the run, and in
answer mode a string or an object raised where each item is read as a turn.
The run job and scripts/eval_retrieval.py read the value through one
function.

FIX cases fail on the code before this change; CONTROL cases pass on both.
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

QUESTION = "tell me more"
TOPIC = "How does the platform keep each department's documents apart?"
REPLY = "Each department has its own collection."
TURNS = [{"role": "user", "content": TOPIC}, {"role": "assistant", "content": REPLY}]
# Stored values that parse, and parse to something that is not a list.
NOT_A_LIST = ["7", '"hi"', "true", '{"a": 1}']
# The two of them the resolver's walk raised on.
RAISED_IN_RETRIEVAL = ["7", "true"]


def _run(monkeypatch, run_id, setup, retrieval_only):
    """One question through the run job; what retrieval was asked with, what
    the writer was sent, and how many rows the run wrote."""
    import app.eval_runner as runner
    from app.db import get_session
    from app.models import EvalResult

    seen = {"queries": [], "messages": []}

    def _retrieve(query, top_k=None, user_level=None, **kw):
        seen["queries"].append(query)
        return []

    def _writer(messages, model, tools=None, system_prompt="", max_tokens=1024, **kw):
        seen["messages"].append([dict(m) for m in messages])
        yield "an answer"

    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("app.rerank.retrieve", _retrieve)
    monkeypatch.setattr(runner, "supports_tools", lambda model="": False)
    monkeypatch.setattr(runner, "stream_chat", _writer)
    for judge in ("judge_answer", "judge_faithfulness", "judge_freshness"):
        monkeypatch.setattr(f"app.eval_judge.{judge}", lambda *a, **k: (1, "ok"))
    questions = [{"id": 9911, "question": QUESTION, "category": "general",
                  "expected_source": "docs/held.md", "notes": "Says more.",
                  "as_level": None, "setup_turns": setup}]
    try:
        runner._run_eval_job(run_id, "2000-01-01T00:00:00", questions, model="m",
                             use_rag=True, n_results=5, retrieval_only=retrieval_only)
        with get_session() as db:
            rows = db.query(EvalResult).filter(EvalResult.run_id == run_id).count()
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    return seen, rows


def _history(messages):
    """The turns the writer was sent between the system prompt and the graded
    question."""
    return [m for m in messages[1:-1] if m.get("role") in ("user", "assistant")]


# -- the read --------------------------------------------------------------------

@pytest.mark.parametrize("raw", NOT_A_LIST + ['"[1]"', "not json", "[", "null", "", None],
                         ids=repr)
def test_fix_a_value_that_is_not_a_json_list_reads_as_no_history(raw):
    from app.eval_runner import _setup_turns
    assert _setup_turns(raw) == []


def test_fix_a_json_list_reads_as_that_list():
    from app.eval_runner import _setup_turns
    assert _setup_turns(json.dumps(TURNS)) == TURNS
    assert _setup_turns("[]") == []


# -- the run job ------------------------------------------------------------------

@pytest.mark.parametrize("raw", NOT_A_LIST, ids=repr)
def test_fix_answer_mode_runs_and_asks_the_question_alone(client, monkeypatch, raw):
    seen, rows = _run(monkeypatch, "setup-read-answer", raw, retrieval_only=False)
    assert rows == 1
    assert seen["queries"] == [QUESTION]
    assert _history(seen["messages"][-1]) == []


@pytest.mark.parametrize("raw", NOT_A_LIST, ids=repr)
def test_retrieval_only_runs_and_retrieves_on_the_question(client, monkeypatch, raw):
    """FIX for a number and true, which raised in the resolver's walk;
    CONTROL for a string and an object, which retrieval already read as no
    turn."""
    seen, rows = _run(monkeypatch, "setup-read-retrieval", raw, retrieval_only=True)
    assert rows == 1
    assert seen["queries"] == [QUESTION]


def test_control_a_list_of_turns_resolves_and_rides_as_history(client, monkeypatch):
    seen, rows = _run(monkeypatch, "setup-read-list", json.dumps(TURNS), retrieval_only=False)
    assert rows == 1
    assert seen["queries"] == [f"{TOPIC} - {QUESTION}"]
    assert _history(seen["messages"][-1]) == TURNS


@pytest.mark.parametrize("raw", [None, "", "null", "[]"], ids=repr)
def test_control_nothing_stored_is_a_single_turn(client, monkeypatch, raw):
    seen, rows = _run(monkeypatch, "setup-read-none", raw, retrieval_only=False)
    assert rows == 1
    assert seen["queries"] == [QUESTION]
    assert _history(seen["messages"][-1]) == []


# -- the offline retrieval script ----------------------------------------------------

def _cohort(monkeypatch, raw):
    import app.rerank as rr
    import eval_retrieval as ev
    seen: list = []
    monkeypatch.setattr(rr, "status", lambda: {"enabled": False, "loaded": False,
                                               "model": "none", "error": None})
    monkeypatch.setattr(ev, "retrieve", lambda query, top_k=None, **kw: seen.append(query) or [])
    monkeypatch.setattr("time.sleep", lambda s: None)
    q = types.SimpleNamespace(id=1, question=QUESTION, expected_source="docs/held.md",
                              category="general", setup_turns=raw)
    out = ev.run_cohort([q], top_k=5, misses_only=False)
    return out, seen


@pytest.mark.parametrize("raw", NOT_A_LIST, ids=repr)
def test_the_script_retrieves_on_the_question(monkeypatch, raw):
    """FIX for a number and true; CONTROL for a string and an object."""
    out, seen = _cohort(monkeypatch, raw)
    assert out is not None and out["scored"] == 1
    assert seen == [QUESTION]


def test_control_the_script_resolves_a_list_of_turns(monkeypatch):
    out, seen = _cohort(monkeypatch, json.dumps(TURNS))
    assert seen == [f"{TOPIC} - {QUESTION}"]
