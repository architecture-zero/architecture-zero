"""A run grades every row with the judge it stamped at its start.

The run stamps its judge once, before the first question (its instrument
stamp, which the trust panel's "Graded by" reads). Each question read the
judge setting again when it was graded, so a setting changed during a run
graded the later rows with a judge the run's stamp does not name.

FIX: the setting changed between the first and the second question of one
run - every rubric of both rows is graded by the run-start judge, and both
rows carry it. This case fails on the code before this change.
CONTROL: with the setting unchanged, every row is graded by and stamped with
the configured judge, as before. This passes on both.
"""
import pytest

from app import eval_runner
from app.config import set_config
from app.db import get_session
from app.models import Config, EvalResult


@pytest.fixture
def judge_setting():
    """Sets the configured judge for one test and puts the row back as it was."""
    with get_session() as db:
        row = db.query(Config).filter(Config.key == "eval_judge_model").first()
        before = row.value if row else None
    yield lambda value: set_config("eval_judge_model", value)
    if before is None:
        with get_session() as db:
            db.query(Config).filter(Config.key == "eval_judge_model").delete()
    else:
        set_config("eval_judge_model", before)


def _questions():
    return [{"id": None, "question": f"What is item {i}?", "category": "general",
             "expected_source": None, "notes": "a key", "holdout": 0,
             "as_level": None, "setup_turns": None} for i in (1, 2)]


def _run(monkeypatch, judge_setting, run_id, move_to=None):
    """Two questions through one run, the writer and the rubrics stubbed.
    With `move_to`, the judge setting changes while the first question is
    graded - after the run started, before the second question. Returns the
    judge each rubric call was handed and the judge each stored row carries."""
    graded = []

    def _rubric(*args, **kwargs):
        graded.append(args[-1])         # every rubric takes the judge model last
        if move_to and len(graded) == 1:
            judge_setting(move_to)
        return 1, "ok"

    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(eval_runner, "supports_tools", lambda m="": False)
    monkeypatch.setattr(eval_runner, "stream_chat", lambda *a, **k: iter(["an answer"]))
    for name in ("judge_answer", "judge_faithfulness", "judge_freshness"):
        monkeypatch.setattr(f"app.eval_judge.{name}", _rubric)
    try:
        eval_runner._run_eval_job(run_id, "2025-01-01T00:00:00", _questions(),
                                  "writer-x", False, 5, False)
        state = eval_runner._eval_runs.get(run_id, {})
        assert not state.get("failed"), state.get("error")
        with get_session() as db:
            stamps = [r.judge_instrument for r in
                      db.query(EvalResult).filter(EvalResult.run_id == run_id)]
    finally:
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
        eval_runner._eval_runs.pop(run_id, None)
    return graded, stamps


def test_fix_a_setting_moved_mid_run_grades_nothing(client, judge_setting, monkeypatch):
    judge_setting("judge-a")
    graded, stamps = _run(monkeypatch, judge_setting, "stamped-judge-moved",
                          move_to="judge-b")
    assert len(graded) == 6 and set(graded) == {"judge-a"}, graded
    assert stamps == ["judge-a", "judge-a"]


def test_control_an_unchanged_setting_grades_as_before(client, judge_setting, monkeypatch):
    judge_setting("judge-a")
    graded, stamps = _run(monkeypatch, judge_setting, "stamped-judge-still")
    assert graded == ["judge-a"] * 6
    assert stamps == ["judge-a", "judge-a"]
