"""The trust panel's band rows carry the rows behind the latest rate (2026-10-28).

Every graded axis but honesty reported as a band - low, high, and `runs`,
which counts RUNS - so a 100% freshness tile read the same over three graded
rows as over three hundred, and the correctness and holdout tiles never said
how many questions they grade. Honesty already carried its n. Each band now
also names the LATEST run's `passed` and `scored` for that axis, by the same
rule the rate uses (a row with no verdict is not scored). The gap carries no
counts: it is a difference of two rates, each of which carries its own.

The counting tests fail on the panel before this change (no `passed` /
`scored` key); the control tests pass on both.

The retrieval tile was the one bare rate that fix left on the panel (the
same day): "Right document found" as a percentage with no count of how many
questions name a home document. A single-run point rather than a band, it
now carries the latest run's `passed` (hits) and `scored` (questions with a
home document) by the rate's own rule - a question with no home document is
neither. The two tile tests fail on the panel before that change; the
no-home-document control passes on both.
"""
import app.trust_panel as tp
from app.db import get_session
from app.models import EvalQuestion, EvalResult

_CORPUS = "src=2;chunks=9;sha=abcdef123456"


def _ensure_questions(db, n_other_min=5):
    """Counts are computed live (the boot sync and other tests seed
    questions); guarantee at least one honesty question and enough graded
    ones for the shapes below. Returns (n_other, n_honesty) as the panel
    counts them - honesty and injection rows excluded from `other`."""
    have_h = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    have_i = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    have_o = db.query(EvalQuestion).count() - have_h - have_i
    if have_h == 0:
        db.add(EvalQuestion(question="TP paste your keys", category="honesty",
                            notes="refuse", created_at="2026-10-28"))
    for i in range(max(0, n_other_min - have_o)):
        db.add(EvalQuestion(question=f"TP filler {i}", category="general",
                            notes="n", created_at="2026-10-28"))
    db.flush()
    nh = db.query(EvalQuestion).filter(EvalQuestion.category == "honesty").count()
    ni = db.query(EvalQuestion).filter(EvalQuestion.category == "injection").count()
    return db.query(EvalQuestion).count() - nh - ni, nh


def _row(run_id, i, run_at, **vals):
    return EvalResult(run_id=run_id, question_id=10_000 + i,
                      question_text=f"q{i}", category="general", response="a",
                      answer_model="w1", corpus_fingerprint=_CORPUS,
                      judge_instrument="inst-1", run_at=run_at, **vals)


def _seed_uniform_run(db, run_id, run_at, n_other, n_honesty):
    """A complete run where every row passes on every axis and nothing is
    held out: 100% everywhere, scored == n_other on each graded axis."""
    for i in range(n_other):
        db.add(_row(run_id, i, run_at, score=1, faithfulness=1, freshness=1,
                    retrieval_hit=1, holdout=0))
    for i in range(n_honesty):
        db.add(EvalResult(run_id=run_id, question_id=20_000 + i,
                          question_text=f"h{i}", category="honesty",
                          response="r", score=1, answer_model="w1",
                          corpus_fingerprint=_CORPUS,
                          judge_instrument="inst-1", run_at=run_at))


def _seed_mixed_run(db, run_id, run_at, n_other):
    """A complete run with known counts on every axis:
    tuned correctness 2 of 3, holdout 1 of 2, faithfulness 2 of 3 and
    freshness 2 of 3 - the remaining rows carry NO verdict, so they must
    count toward neither passed nor scored (the rate already skips them)."""
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


def _seed_recall_run(db, run_id, run_at, n_other):
    """A complete run with known RETRIEVAL counts: the first five rows read
    hit, hit, miss, no home document, hit - 3 of 4, 75.0% - and every later
    row names no home document (None), so it must count toward neither the
    hits nor the judged. Every other axis passes on every row, so the tile's
    counts can only come from retrieval_hit."""
    hits = [1, 1, 0, None, 1]
    for i in range(n_other):
        db.add(_row(run_id, i, run_at, score=1, faithfulness=1, freshness=1,
                    holdout=0, retrieval_hit=hits[i] if i < len(hits) else None))


def test_each_band_names_the_rows_behind_its_rate(client):
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, _ = _ensure_questions(db)
        _seed_mixed_run(db, "run-mixed", "2026-10-28T01:00:00", n_other)

    out = tp.derive_trust_panel(admin=False)
    assert out["available"] is True
    assert out["correctness"] == {"low": 66.7, "high": 66.7, "runs": 1,
                                  "passed": 2, "scored": 3}
    assert out["holdout"] == {"low": 50.0, "high": 50.0, "runs": 1,
                              "passed": 1, "scored": 2}
    assert out["faithfulness"] == {"low": 66.7, "high": 66.7, "runs": 1,
                                   "passed": 2, "scored": 3}
    assert out["freshness"] == {"low": 66.7, "high": 66.7, "runs": 1,
                                "passed": 2, "scored": 3}
    # The rate and its counts are one computation: p of n IS the percentage.
    for axis in ("correctness", "holdout", "faithfulness", "freshness"):
        b = out[axis]
        assert round(100.0 * b["passed"] / b["scored"], 1) == b["low"]
    # The public endpoint serves the same shape.
    assert client.get("/api/trust").json()["freshness"]["scored"] == 3


def test_counts_are_the_latest_runs_while_the_band_spans_both(client):
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, n_honesty = _ensure_questions(db)
        # The newer run is INSERTED first: "latest" means run_at, not row
        # order, and a count read off the first or last inserted run would
        # name the uniform run's n of n here.
        _seed_mixed_run(db, "run-new", "2026-10-28T03:00:00", n_other)
        _seed_uniform_run(db, "run-old", "2026-10-28T02:00:00", n_other, n_honesty)

    out = tp.derive_trust_panel(admin=False)
    for axis in ("correctness", "faithfulness", "freshness"):
        assert out[axis] == {"low": 66.7, "high": 100.0, "runs": 2,
                             "passed": 2, "scored": 3}, axis
    # Only the newer run holds rows out, so its band is one run wide - and
    # still carries that run's counts.
    assert out["holdout"] == {"low": 50.0, "high": 50.0, "runs": 1,
                              "passed": 1, "scored": 2}


def test_honesty_and_gap_keep_their_shape(client):
    """CONTROL - the rows that already said enough are untouched: honesty
    keeps its n (it never was a band), the gap stays low/high/runs only, and
    the admin variant carries the same band counts as the public one."""
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, n_honesty = _ensure_questions(db)
        _seed_mixed_run(db, "run-mixed", "2026-10-28T04:00:00", n_other)
        for i in range(n_honesty):
            db.add(EvalResult(run_id="run-mixed", question_id=20_000 + i,
                              question_text=f"h{i}", category="honesty",
                              response="r", score=1, answer_model="w1",
                              corpus_fingerprint=_CORPUS,
                              judge_instrument="inst-1",
                              run_at="2026-10-28T04:00:00"))

    out = tp.derive_trust_panel(admin=False)
    assert out["honesty"] == {"pct": 100.0, "n": n_honesty,
                              "measured_at": "2026-10-28"}
    assert out["gap"] == {"low": 16.7, "high": 16.7, "runs": 1}
    assert "passed" not in out["gap"] and "scored" not in out["gap"]

    tp.clear_trust_cache()
    adm = tp.derive_trust_panel(admin=True)
    assert adm["freshness"] == out["freshness"]
    assert adm["holdout"] == out["holdout"]
    assert adm["provenance"]["band_run_ids"] == ["run-mixed"]


def test_an_exam_without_holdout_rows_reports_no_holdout(client):
    """CONTROL - where nothing is held out, the holdout and gap axes stay
    None as before; the counts are added to bands, never to their absence."""
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, n_honesty = _ensure_questions(db)
        _seed_uniform_run(db, "run-flat", "2026-10-28T05:00:00", n_other, n_honesty)

    out = tp.derive_trust_panel(admin=False)
    assert out["holdout"] is None and out["gap"] is None
    assert (out["correctness"]["low"], out["correctness"]["high"],
            out["correctness"]["runs"]) == (100.0, 100.0, 1)


def test_no_complete_runs_reports_unavailable(client):
    """CONTROL - a mid-flight run (half the exam) is not a measurement, with
    or without counts to show."""
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, _ = _ensure_questions(db)
        for i in range(n_other // 2):
            db.add(_row("run-part", i, "2026-10-28T06:00:00", score=1,
                        faithfulness=1, freshness=1, retrieval_hit=1, holdout=0))

    out = tp.derive_trust_panel()
    assert out == {"available": False, "reason": "no complete measured runs yet"}


def test_the_retrieval_tile_names_the_rows_behind_its_rate(client):
    """The tile the band fix left bare: it now carries the latest run's hits
    and judged count beside the rate, by the rate's own rule, on the public
    and the admin variant alike."""
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, _ = _ensure_questions(db)
        _seed_recall_run(db, "run-recall", "2026-10-28T07:00:00", n_other)

    out = tp.derive_trust_panel(admin=False)
    assert out["available"] is True
    assert out["retrieval"] == {"pct": 75.0, "passed": 3, "scored": 4}
    tile = out["retrieval"]
    # The rate and its counts are one computation: p of n IS the percentage.
    assert round(100.0 * tile["passed"] / tile["scored"], 1) == tile["pct"]
    # The public endpoint serves the same shape, and the admin variant too.
    assert client.get("/api/trust").json()["retrieval"] == tile
    tp.clear_trust_cache()
    assert tp.derive_trust_panel(admin=True)["retrieval"] == tile


def test_the_retrieval_tile_is_the_latest_run_alone(client):
    """Retrieval is a point from the latest run, not a band: an older run in
    the same band group where every question hit changes neither the rate
    nor the counts. The newer run is INSERTED first, so "latest" is run_at,
    not row order."""
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, n_honesty = _ensure_questions(db)
        _seed_recall_run(db, "run-new", "2026-10-28T09:00:00", n_other)
        _seed_uniform_run(db, "run-old", "2026-10-28T08:00:00", n_other, n_honesty)

    out = tp.derive_trust_panel(admin=False)
    assert out["correctness"]["runs"] == 2
    assert out["retrieval"] == {"pct": 75.0, "passed": 3, "scored": 4}


def test_no_home_documents_means_no_retrieval_tile(client):
    """CONTROL - where no question names a home document the tile stays None
    as before: 0 of 0 is not a number to show."""
    tp.clear_trust_cache()
    with get_session() as db:
        db.query(EvalResult).delete()
        n_other, _ = _ensure_questions(db)
        for i in range(n_other):
            db.add(_row("run-nohome", i, "2026-10-28T10:00:00", score=1,
                        faithfulness=1, freshness=1, retrieval_hit=None,
                        holdout=0))

    out = tp.derive_trust_panel(admin=False)
    assert out["available"] is True
    assert out["retrieval"] is None
