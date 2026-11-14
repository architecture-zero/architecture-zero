"""The admin provenance's "Graded by" is the judge that graded the judged run.

Every judged run stamps the judge model it was graded by (its instrument stamp,
eval_runner). The provenance read today's configured judge instead, so after
the setting moved to another judge with no new judged run, "Graded by" named a
model that graded none of the numbers shown beside it.

FIX: the provenance names the judged run's own stamp; a run with no stamp
answers None, which the page shows as "not recorded" - never today's setting
in its place. These cases fail on the code before this change.
CONTROL: a run graded by the judge configured today reads as before, and no
tile's number moves with the setting. These pass on both.
"""
import pytest

import app.trust_panel as tp
from app.config import set_config
from app.db import get_session
from app.models import Config, EvalQuestion, EvalResult

_CORPUS = "src=2;chunks=9;sha=abcdef123456"
_JUDGED_AT = "2025-01-02T11:00:00"


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


def _ensure_questions(db, n_other_min=5):
    have_h = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    have_i = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    have_o = db.query(EvalQuestion).count() - have_h - have_i
    for i in range(max(0, n_other_min - have_o)):
        db.add(EvalQuestion(question=f"JR filler {i}", category="general",
                            notes="n", created_at="2025-01-01"))
    db.flush()
    nh = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    ni = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    return db.query(EvalQuestion).count() - nh - ni


def _judged_run(judge):
    """One complete judged run (no honesty rows) graded by `judge`, as its
    instrument stamp records it; correctness 2 of 3 and faithfulness and
    freshness 1 of 1 on the graded rows."""
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other = _ensure_questions(db)
        scores = [1, 1, 0]
        for i in range(n_other):
            graded = i < len(scores)
            db.add(EvalResult(run_id="jr-judged", question_id=10_000 + i,
                              question_text=f"q{i}", category="general", response="a",
                              answer_model="w1", corpus_fingerprint=_CORPUS,
                              judge_instrument=judge, run_setup="setup-1",
                              run_at=_JUDGED_AT, holdout=0, retrieval_hit=1,
                              score=scores[i] if graded else None,
                              faithfulness=1 if i == 0 else None,
                              freshness=1 if i == 0 else None))


def _admin():
    tp.clear_trust_cache()
    return tp.derive_trust_panel(admin=True)


# -- FIX ---------------------------------------------------------------------------

def test_fix_graded_by_is_the_runs_judge_after_the_setting_moves(client, judge_setting):
    _judged_run("judge-a")
    judge_setting("judge-b")
    assert _admin()["provenance"]["judge"] == "judge-a"


def test_fix_a_run_with_no_stamp_says_none_never_todays_setting(client, judge_setting):
    _judged_run(None)
    judge_setting("judge-b")
    assert _admin()["provenance"]["judge"] is None


# -- CONTROL -------------------------------------------------------------------------

def test_control_a_run_graded_by_todays_judge_reads_as_before(client, judge_setting):
    _judged_run("judge-a")
    judge_setting("judge-a")
    assert _admin()["provenance"]["judge"] == "judge-a"


def test_control_no_tiles_number_moves_with_the_setting(client, judge_setting):
    _judged_run("judge-a")
    judge_setting("judge-a")
    before = {k: v for k, v in _admin().items() if k != "provenance"}
    judge_setting("judge-b")
    after = {k: v for k, v in _admin().items() if k != "provenance"}
    assert after == before
    assert after["correctness"] == {"low": 66.7, "high": 66.7, "runs": 1,
                                    "passed": 2, "scored": 3}
