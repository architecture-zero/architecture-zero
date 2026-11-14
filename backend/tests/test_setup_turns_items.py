"""A stored list of setup turns keeps only its well-formed turns, the
resolver's walk skips a turn whose content is not text, and the seed sync
refuses a setup_turns that is not a list.

1. eval_runner._setup_turns keeps an item only when it is an object whose
   role is one the chat's history takes (user or assistant) and whose content
   is text. A list of good turns comes back as stored, so no existing seed
   sends anything different. Before, every item reached the run: in answer
   mode a number or a string raised where each item is read as a turn, an
   object without content or with another role was sent to the writer as
   history, and a user turn whose content was not text raised in the
   resolver's walk in every mode.
2. routing._last_topic_text skips a turn whose content is not text instead of
   calling strip() on it. The chat keeps its own guard: its history model
   takes text only.
3. sync_eval_questions_from_seed refuses a row whose setup_turns is present
   and not a list, the way it already handles a repeated question: the row is
   skipped and named in the result (with its place in the file and, for a
   question already in the table, its id) and in the sync's log line, an
   error line says so with the rule, and the rest of the file syncs. The
   value is never stored.

FIX cases fail on the code before this change; CONTROL cases pass on both.
"""
import json
import os
import sys
import types
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers every table on Base
from app.db import Base

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scripts")))

QUESTION = "tell me more"
TOPIC = "How does the platform keep each department's documents apart?"
REPLY = "Each department has its own collection."
GOOD = [{"role": "user", "content": TOPIC}, {"role": "assistant", "content": REPLY}]
# Items that are not turns, each set between the two good ones.
BAD = {
    "a number": 7,
    "a string": "hi",
    "no content": {"role": "user"},
    "content a number": {"role": "user", "content": 5},
    "content a list": {"role": "user", "content": ["x"]},
    "another role": {"role": "system", "content": "x"},
    "no role": {"content": "x"},
}
# The two the resolver's walk raised on (a user turn whose content is not
# text); the walk passed over every other one already.
RAISED_IN_THE_WALK = ["content a number", "content a list"]


def _mixed(name):
    return json.dumps([GOOD[0], BAD[name], GOOD[1]])


# -- 1. the read ------------------------------------------------------------------

@pytest.mark.parametrize("name", list(BAD))
def test_fix_an_item_that_is_not_a_turn_is_dropped(name):
    from app.eval_runner import _setup_turns
    assert _setup_turns(_mixed(name)) == GOOD


def test_control_a_list_of_good_turns_comes_back_as_stored():
    from app.eval_runner import _setup_turns
    stored = GOOD + [{"role": "user", "content": "", "note": "kept whole"}]
    assert _setup_turns(json.dumps(stored)) == stored


def _run(monkeypatch, run_id, setup, retrieval_only):
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
    questions = [{"id": 9921, "question": QUESTION, "category": "general",
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
    """Everything the writer was sent between the system prompt and the
    graded question: the scripted turns, whatever their shape."""
    return messages[1:-1]


@pytest.mark.parametrize("name", list(BAD))
def test_fix_answer_mode_runs_on_the_good_turns_only(client, monkeypatch, name):
    seen, rows = _run(monkeypatch, "setup-items-answer", _mixed(name), retrieval_only=False)
    assert rows == 1
    assert seen["queries"] == [f"{TOPIC} - {QUESTION}"]
    assert _history(seen["messages"][-1]) == GOOD


@pytest.mark.parametrize("name", list(BAD))
def test_retrieval_only_runs_on_the_good_turns(client, monkeypatch, name):
    """FIX for a user turn whose content is not text (the walk raised on it);
    CONTROL for the other items, which the walk already passed over."""
    seen, rows = _run(monkeypatch, "setup-items-retrieval", _mixed(name), retrieval_only=True)
    assert rows == 1
    assert seen["queries"] == [f"{TOPIC} - {QUESTION}"]


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
    return ev.run_cohort([q], top_k=5, misses_only=False), seen


@pytest.mark.parametrize("name", list(BAD))
def test_the_script_runs_on_the_good_turns(monkeypatch, name):
    """FIX for a user turn whose content is not text; CONTROL for the rest."""
    out, seen = _cohort(monkeypatch, _mixed(name))
    assert out is not None and out["scored"] == 1
    assert seen == [f"{TOPIC} - {QUESTION}"]


# -- 2. the walk -------------------------------------------------------------------

@pytest.mark.parametrize("content", [5, ["x"], {"a": 1}, True], ids=repr)
def test_fix_the_walk_skips_a_turn_whose_content_is_not_text(content):
    from app.routing import _last_topic_text, resolve_followup
    history = [GOOD[0], {"role": "user", "content": content}]
    assert _last_topic_text(history) == TOPIC
    obj = types.SimpleNamespace(role="user", content=content)
    assert _last_topic_text([GOOD[0], obj]) == TOPIC
    assert resolve_followup(QUESTION, history) == f"{TOPIC} - {QUESTION}"


def test_control_a_chat_history_reads_as_before():
    from app.routers.chat import Message
    from app.routing import _last_topic_text, resolve_followup
    history = [Message(role="user", content=TOPIC), Message(role="assistant", content=REPLY)]
    assert _last_topic_text(history) == TOPIC
    assert resolve_followup(QUESTION, history) == f"{TOPIC} - {QUESTION}"


def test_control_the_chat_history_takes_text_only():
    from pydantic import ValidationError
    from app.routers.chat import Message
    with pytest.raises(ValidationError):
        Message(role="user", content=5)


# -- 3. the seed sync ---------------------------------------------------------------

@pytest.fixture
def fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    @contextmanager
    def get_session():
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    yield get_session
    engine.dispose()


def _sync(monkeypatch, fresh_session, tmp_path, rows):
    import app.eval_runner as runner
    errors: list = []
    path = tmp_path / "eval-seed.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", str(path))
    monkeypatch.setattr(runner, "log_error", lambda event, **f: errors.append((event, f)))
    return runner.sync_eval_questions_from_seed(), errors


def _stored(fresh_session):
    from app.models import EvalQuestion
    with fresh_session() as db:
        return {r.question: (r.id, r.setup_turns) for r in db.query(EvalQuestion).all()}


@pytest.mark.parametrize("setup", [7, "hi", {}], ids=repr)
def test_fix_a_new_row_whose_setup_turns_is_not_a_list_is_refused(
        monkeypatch, fresh_session, tmp_path, setup):
    rows = [{"question": "Q before?"},
            {"question": "Q bad?", "setup_turns": setup},
            {"question": "Q after?", "setup_turns": GOOD}]
    result, errors = _sync(monkeypatch, fresh_session, tmp_path, rows)
    assert result["added"] == 2
    (refused,) = result["refused"]
    assert (refused["index"], refused["id"], refused["question"]) == (1, None, "Q bad?")
    assert "JSON list" in refused["rule"]
    assert errors == [("eval_seed_row_refused",
                       {"index": 1, "question_id": None, "rule": refused["rule"]})]
    stored = _stored(fresh_session)
    assert "Q bad?" not in stored
    assert json.loads(stored["Q after?"][1]) == GOOD


@pytest.mark.parametrize("setup", [7, "hi", {}], ids=repr)
def test_fix_an_existing_row_is_named_by_its_id_and_keeps_what_it_had(
        monkeypatch, fresh_session, tmp_path, setup):
    first, _ = _sync(monkeypatch, fresh_session, tmp_path,
                     [{"question": "Q kept?", "setup_turns": GOOD}])
    assert first["added"] == 1
    qid, before = _stored(fresh_session)["Q kept?"]
    result, errors = _sync(monkeypatch, fresh_session, tmp_path,
                           [{"question": "Q kept?", "category": "changed",
                             "setup_turns": setup}])
    (refused,) = result["refused"]
    assert (refused["index"], refused["id"]) == (0, qid)
    assert errors[0][1]["question_id"] == qid
    assert (result["updated"], result["db_only"]) == (0, [])
    assert _stored(fresh_session)["Q kept?"] == (qid, before)


def test_control_a_list_and_an_absent_value_sync_as_before(monkeypatch, fresh_session, tmp_path):
    rows = [{"question": "Q list?", "setup_turns": GOOD},
            {"question": "Q none?"},
            {"question": "Q null?", "setup_turns": None},
            {"question": "Q empty?", "setup_turns": []}]
    result, errors = _sync(monkeypatch, fresh_session, tmp_path, rows)
    assert result["added"] == 4
    assert result.get("refused", []) == [] and errors == []
    stored = _stored(fresh_session)
    assert json.loads(stored["Q list?"][1]) == GOOD
    assert stored["Q none?"][1] is None and stored["Q null?"][1] is None
    assert stored["Q empty?"][1] is None
