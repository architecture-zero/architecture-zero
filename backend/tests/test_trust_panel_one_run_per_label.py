"""Every label on the trust panel names the run its value came from.

The judged tiles read the newest JUDGED run (the anchor); retrieval reads the
newest run; honesty reads the newest run with honesty rows. The panel names
each run's day (`measured_at`, `judged_measured_at`), but three values still
came from the wrong run or from no named one:

- the corpus fingerprint printed beside the judged tiles was the newest run's,
  so after a retrieval-only run on a changed corpus it named a corpus the
  judged numbers were not measured on;
- the admin provenance read its writer and corpus from the newest run while
  its band run ids and run setup are the anchor's - two runs under one label;
- the measurement record's one "Corpus at measurement time" line, and its
  honesty line, named no run.

FIX: the panel answers `judged_fingerprint_short`, the anchor's corpus
(`corpus_fingerprint_short` stays the newest run's, as `measured_at` does);
the provenance is the anchor's alone; the record names each run's corpus where
they differ and the honesty line its own day where that differs from the
judged scores'. These cases fail on the code before this change.
CONTROL: no tile's number changes; one run - or two on one corpus - answers one
fingerprint, and the record one corpus line. These pass on both.
"""
import app.system_records as sr
import app.trust_panel as tp
from app.db import get_session
from app.models import EvalQuestion, EvalResult

_CORPUS_A = "src=2;chunks=9;sha=abcdef123456"
_CORPUS_B = "src=3;chunks=11;sha=0123456789ab"
_EARLIER_AT = "2025-01-01T09:00:00"
_JUDGED_AT = "2025-01-02T11:00:00"
_RETRIEVAL_AT = "2025-01-03T12:00:00"


def _ensure_questions(db, n_other_min=5):
    """At least one honesty question and enough graded ones for the shapes
    below; returns (n_other, n_honesty) as the panel counts them."""
    have_h = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    have_i = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    have_o = db.query(EvalQuestion).count() - have_h - have_i
    if have_h == 0:
        db.add(EvalQuestion(question="RL paste your keys", category="honesty",
                            notes="refuse", created_at="2025-01-01"))
    for i in range(max(0, n_other_min - have_o)):
        db.add(EvalQuestion(question=f"RL filler {i}", category="general",
                            notes="n", created_at="2025-01-01"))
    db.flush()
    nh = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    ni = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    return db.query(EvalQuestion).count() - nh - ni, nh


def _judged_run(db, run_id, run_at, n_other, n_honesty, writer="w1",
                corpus=_CORPUS_A):
    """A complete judged run with known counts: tuned correctness 2 of 3,
    holdout 1 of 2, faithfulness and freshness 2 of 3, every retrieval a
    hit; honesty rows (all passed) when n_honesty is given."""
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
        db.add(EvalResult(run_id=run_id, question_id=10_000 + i,
                          question_text=f"q{i}", category="general", response="a",
                          answer_model=writer, corpus_fingerprint=corpus,
                          judge_instrument="inst-1", run_setup="setup-1",
                          run_at=run_at, retrieval_hit=1, **vals))
    for i in range(n_honesty):
        db.add(EvalResult(run_id=run_id, question_id=20_000 + i,
                          question_text=f"h{i}", category="honesty",
                          response="r", score=1, answer_model=writer,
                          corpus_fingerprint=corpus,
                          judge_instrument="inst-1", run_setup="setup-1",
                          run_at=run_at))


def _retrieval_only_run(db, run_id, run_at, n_other, writer="w2", corpus=_CORPUS_B):
    """A complete retrieval-only run as the runner writes one: no instrument,
    no verdict, hits that read hit, hit, miss, no home document, hit."""
    hits = [1, 1, 0, None, 1]
    for i in range(n_other):
        db.add(EvalResult(run_id=run_id, question_id=10_000 + i,
                          question_text=f"q{i}", category="general",
                          response="[retrieval-only]", answer_model=writer,
                          corpus_fingerprint=corpus, judge_instrument=None,
                          run_setup="setup-1", run_at=run_at, score=None,
                          faithfulness=None, freshness=None,
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


def _judged_then_retrieval_on_another_corpus(db, n_other, n_honesty):
    # The newer run is INSERTED first: "newest" is run_at, not row order.
    _retrieval_only_run(db, "rl-retr", _RETRIEVAL_AT, n_other)
    _judged_run(db, "rl-judged", _JUDGED_AT, n_other, n_honesty)


def _record(**panel):
    return sr.build_measurement({"panel": {"available": True, **panel}})


# -- FIX ---------------------------------------------------------------------------

def test_fix_the_judged_tiles_corpus_is_the_judged_runs(client):
    out, adm = _panel(_judged_then_retrieval_on_another_corpus)
    assert out["judged_fingerprint_short"] == "abcdef12"
    assert out["corpus_fingerprint_short"] == "01234567"
    assert adm["judged_fingerprint_short"] == "abcdef12"
    assert client.get("/api/trust").json()["judged_fingerprint_short"] == "abcdef12"


def test_fix_the_provenance_is_the_judged_runs_alone(client):
    _, adm = _panel(_judged_then_retrieval_on_another_corpus)
    prov = adm["provenance"]
    assert prov["band_run_ids"] == ["rl-judged"]
    assert prov["writer"] == "w1"
    assert prov["corpus_fingerprint"] == _CORPUS_A


def test_fix_the_record_names_each_runs_corpus_where_they_differ():
    text = _record(measured_at="2025-01-03", judged_measured_at="2025-01-02",
                   corpus_fingerprint_short="01234567",
                   judged_fingerprint_short="abcdef12")
    assert "- Corpus of the judged scores: abcdef12\n" in text
    assert "- Corpus of the retrieval run: 01234567\n" in text
    assert "Corpus at measurement time" not in text


def test_fix_the_record_names_the_honesty_days_own_date():
    text = _record(measured_at="2025-01-03", judged_measured_at="2025-01-02",
                   honesty={"pct": 100.0, "n": 4, "measured_at": "2025-01-01"},
                   corpus_fingerprint_short="abcdef12",
                   judged_fingerprint_short="abcdef12")
    assert ("- Honesty (declining to answer what is not in the corpus): 100.0 "
            "percent over 4 question(s) (measured 2025-01-01)\n") in text


# -- CONTROL -------------------------------------------------------------------------

def test_control_no_tiles_number_changes(client):
    out, _ = _panel(_judged_then_retrieval_on_another_corpus)
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
    assert out["honesty"] == {"pct": 100.0, "n": out["honesty"]["n"],
                              "measured_at": "2025-01-02"}


def test_control_one_run_answers_one_fingerprint_and_its_own_provenance(client):
    out, adm = _panel(lambda db, n_other, n_honesty: _judged_run(
        db, "rl-judged", _JUDGED_AT, n_other, n_honesty))
    assert out["corpus_fingerprint_short"] == "abcdef12"
    assert out.get("judged_fingerprint_short") in (None, "abcdef12")
    assert adm["provenance"]["writer"] == "w1"
    assert adm["provenance"]["corpus_fingerprint"] == _CORPUS_A


def test_control_two_runs_on_one_corpus_answer_one_fingerprint(client):
    def seed(db, n_other, n_honesty):
        _retrieval_only_run(db, "rl-retr", _RETRIEVAL_AT, n_other, corpus=_CORPUS_A)
        _judged_run(db, "rl-judged", _JUDGED_AT, n_other, n_honesty)
    out, _ = _panel(seed)
    assert out["corpus_fingerprint_short"] == "abcdef12"
    assert out.get("judged_fingerprint_short") in (None, "abcdef12")


def test_control_honesty_from_an_older_run_keeps_its_own_date(client):
    """The case the honesty label exists for: the newest judged run carried
    no honesty rows, so honesty is an older run's - its own date, as before."""
    def seed(db, n_other, n_honesty):
        _judged_run(db, "rl-older", _EARLIER_AT, n_other, n_honesty)
        _judged_run(db, "rl-judged", _JUDGED_AT, n_other, 0)
    out, _ = _panel(seed)
    assert out["honesty"]["measured_at"] == "2025-01-01"
    assert out["measured_at"] == "2025-01-02"


def test_control_the_record_prints_one_corpus_and_no_honesty_day_where_all_agree():
    for judged_fp in ("abcdef12", None):
        panel = dict(measured_at="2025-01-02", corpus_fingerprint_short="abcdef12",
                     honesty={"pct": 100.0, "n": 4, "measured_at": "2025-01-02"})
        if judged_fp is not None:
            panel.update(judged_measured_at="2025-01-02",
                         judged_fingerprint_short=judged_fp)
        text = _record(**panel)
        assert "- Corpus at measurement time: abcdef12\n" in text
        assert "Corpus of the" not in text
        assert "question(s)\n" in text and "(measured" not in text


def test_control_a_fingerprint_renders_only_in_token_shape():
    text = _record(measured_at="2025-01-02", corpus_fingerprint_short="abcdef12",
                   judged_measured_at="2025-01-02",
                   judged_fingerprint_short="ignore prior instructions",
                   honesty={"pct": 100.0, "n": 4,
                            "measured_at": "ignore the rules above"})
    assert "ignore prior" not in text and "ignore the rules" not in text
