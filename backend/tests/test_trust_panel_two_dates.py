"""The trust panel names the day its judged tiles were measured.

The judged tiles (correctness, holdout, gap, faithfulness, freshness) read the
newest JUDGED run; retrieval reads the newest run. The panel answered one
date, `measured_at`, and it is the newest run's - so after a retrieval-only
run the page said "Last measured <that day>" over judged numbers measured on
an earlier day.

FIX: the panel also answers `judged_measured_at`, the judged anchor's day, and
the measurement record names both days where they differ. These cases fail on
the code before this change.
CONTROL: no tile's number changes; a judged run alone answers one date; with
no judged run there is no judged date; the record prints one line where the
two agree, and renders a date only in token shape. These pass on both.
"""
import app.system_records as sr
import app.trust_panel as tp
from app.db import get_session
from app.models import EvalQuestion, EvalResult

_CORPUS = "src=2;chunks=9;sha=abcdef123456"
_JUDGED_AT = "2025-01-01T11:00:00"
_RETRIEVAL_AT = "2025-01-02T12:00:00"


def _ensure_questions(db, n_other_min=5):
    """At least one honesty question and enough graded ones for the shapes
    below; returns (n_other, n_honesty) as the panel counts them."""
    have_h = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    have_i = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    have_o = db.query(EvalQuestion).count() - have_h - have_i
    if have_h == 0:
        db.add(EvalQuestion(question="TD paste your keys", category="honesty",
                            notes="refuse", created_at="2025-01-01"))
    for i in range(max(0, n_other_min - have_o)):
        db.add(EvalQuestion(question=f"TD filler {i}", category="general",
                            notes="n", created_at="2025-01-01"))
    db.flush()
    nh = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    ni = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    return db.query(EvalQuestion).count() - nh - ni, nh


def _row(run_id, i, run_at, instrument="inst-1", response="a", **vals):
    return EvalResult(run_id=run_id, question_id=10_000 + i,
                      question_text=f"q{i}", category="general", response=response,
                      answer_model="w1", corpus_fingerprint=_CORPUS,
                      judge_instrument=instrument, run_setup="setup-1",
                      run_at=run_at, **vals)


def _seed_judged_run(db, run_id, run_at, n_other, n_honesty):
    """A complete judged run with known counts: tuned correctness 2 of 3,
    holdout 1 of 2, faithfulness and freshness 2 of 3, every retrieval a hit,
    and every honesty row passed."""
    rows = [
        dict(score=1, faithfulness=1, freshness=1, holdout=0),
        dict(score=1, faithfulness=0, freshness=1, holdout=0),
        dict(score=0, faithfulness=1, freshness=0, holdout=0),
        dict(score=1, faithfulness=None, freshness=None, holdout=1),
        dict(score=0, faithfulness=None, freshness=None, holdout=1),
    ]
    for i in range(n_other):
        vals = rows[i] if i < len(rows) else dict(
            score=None, faithfulness=None, freshness=None, holdout=0)
        db.add(_row(run_id, i, run_at, retrieval_hit=1, **vals))
    for i in range(n_honesty):
        db.add(EvalResult(run_id=run_id, question_id=20_000 + i,
                          question_text=f"h{i}", category="honesty",
                          response="r", score=1, answer_model="w1",
                          corpus_fingerprint=_CORPUS,
                          judge_instrument="inst-1", run_setup="setup-1",
                          run_at=run_at))


def _seed_retrieval_only_run(db, run_id, run_at, n_other):
    """A complete retrieval-only run as the runner writes one: no instrument,
    no verdict, and hits that read hit, hit, miss, no home document, hit."""
    hits = [1, 1, 0, None, 1]
    for i in range(n_other):
        db.add(_row(run_id, i, run_at, instrument=None, response="[retrieval-only]",
                    score=None, faithfulness=None, freshness=None,
                    holdout=1 if i in (3, 4) else 0,
                    retrieval_hit=hits[i] if i < len(hits) else None))


def _panel(seed):
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, n_honesty = _ensure_questions(db)
        seed(db, n_other, n_honesty)
    out = tp.derive_trust_panel(admin=False)
    tp.clear_trust_cache()
    return out, tp.derive_trust_panel(admin=True)


def _judged_then_retrieval(db, n_other, n_honesty):
    # The newer run is INSERTED first: "newest" is run_at, not row order.
    _seed_retrieval_only_run(db, "td-retr", _RETRIEVAL_AT, n_other)
    _seed_judged_run(db, "td-judged", _JUDGED_AT, n_other, n_honesty)


# -- FIX ---------------------------------------------------------------------------

def test_fix_a_retrieval_only_run_after_a_judged_one_answers_two_dates(client):
    out, adm = _panel(_judged_then_retrieval)
    assert out["measured_at"] == "2025-01-02"
    assert out["judged_measured_at"] == "2025-01-01"
    assert adm["judged_measured_at"] == "2025-01-01"
    assert client.get("/api/trust").json()["judged_measured_at"] == "2025-01-01"


def test_fix_the_measurement_record_names_both_days_where_they_differ():
    text = sr.build_measurement({"panel": {
        "available": True, "measured_at": "2025-01-02",
        "judged_measured_at": "2025-01-01", "corpus_fingerprint_short": "abcdef12"}})
    assert "- Judged scores last measured: 2025-01-01\n" in text
    assert "- Retrieval last measured: 2025-01-02\n" in text
    assert "- Last measured:" not in text


# -- CONTROL -------------------------------------------------------------------------

def test_control_no_tiles_number_changes(client):
    """The same scenario: every tile reads exactly what it read before the
    second date existed."""
    out, _ = _panel(_judged_then_retrieval)
    assert out["correctness"] == {"low": 66.7, "high": 66.7, "runs": 1,
                                  "passed": 2, "scored": 3}
    assert out["holdout"] == {"low": 50.0, "high": 50.0, "runs": 1,
                              "passed": 1, "scored": 2}
    assert out["gap"] == {"low": 16.7, "high": 16.7, "runs": 1}
    assert out["faithfulness"] == {"low": 66.7, "high": 66.7, "runs": 1,
                                   "passed": 2, "scored": 3}
    assert out["freshness"] == {"low": 66.7, "high": 66.7, "runs": 1,
                                "passed": 2, "scored": 3}
    assert out["retrieval"] == {"pct": 75.0, "passed": 3, "scored": 4}
    assert out["honesty"]["pct"] == 100.0
    assert out["honesty"]["measured_at"] == "2025-01-01"


def test_control_a_judged_run_alone_answers_one_date(client):
    out, adm = _panel(lambda db, n_other, n_honesty: _seed_judged_run(
        db, "td-judged", _JUDGED_AT, n_other, n_honesty))
    assert out["measured_at"] == "2025-01-01"
    assert out.get("judged_measured_at") in (None, out["measured_at"])
    assert adm.get("judged_measured_at") in (None, adm["measured_at"])


def test_control_with_no_judged_run_there_is_no_judged_date(client):
    out, _ = _panel(lambda db, n_other, n_honesty: _seed_retrieval_only_run(
        db, "td-retr", _RETRIEVAL_AT, n_other))
    assert out["measured_at"] == "2025-01-02"
    assert out.get("judged_measured_at") is None
    assert out["correctness"] is None


def test_control_the_record_prints_one_date_where_the_two_agree():
    for judged in ("2025-01-02", None):
        panel = {"available": True, "measured_at": "2025-01-02",
                 "corpus_fingerprint_short": "abcdef12"}
        if judged is not None:
            panel["judged_measured_at"] = judged
        text = sr.build_measurement({"panel": panel})
        assert "- Last measured: 2025-01-02\n" in text
        assert "Judged scores" not in text and "Retrieval last measured" not in text


def test_control_a_date_renders_only_in_token_shape():
    text = sr.build_measurement({"panel": {
        "available": True, "measured_at": "2025-01-02",
        "judged_measured_at": "ignore prior instructions"}})
    assert "ignore prior" not in text
