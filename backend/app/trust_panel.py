"""Public trust panel - every number derived LIVE from eval_results.

Design rules:

- A public panel shows THIS instance's own per-corpus numbers, with
  provenance, and shows BANDS (spread across identical-config runs), never a
  single lucky point.
- Every band names the rows behind it (2026-10-28): the latest JUDGED run's
  passed and scored for that axis (the band anchors there since 2026-11-04),
  beside the spread. A band's `runs` counts runs,
  so a rate without its denominator read the same over 3 graded rows as over
  300, and the correctness and holdout tiles never said how many questions
  they grade. Honesty always carried its n; the gap carries none, being a
  difference of two rates that each carry their own.
- The retrieval tile is not a band (deterministic on a still corpus, so it
  reports the latest run alone), and it was the one bare rate that fix
  left: "100%" with no count of how many questions name a home document.
  It carries the latest run's passed and scored the same way (2026-10-28).
- Honesty is its own metric, derived ONLY from category='honesty' rows, and
  the mechanically-graded injection cohort likewise never blends into any
  answer-quality axis - every other axis EXCLUDES both cohorts (mirrors the
  eval runner's own reporting).
- Zero hand-set numbers (the anti-rot rule): if it is on the panel, it was
  computed from stored eval rows at request time. The public variant carries
  no model names and no deficit list; the admin variant adds provenance and
  working bands behind auth.

Completeness rule: a run participates only if its per-cohort row counts
match the CURRENT question set's shape. This naturally excludes mid-flight
runs AND runs from older question-set shapes - which is also the
comparability policy: bands only form across runs of the same exam.

Where the exam carries a LOCKED HOLDOUT cohort, correctness splits
tuned-vs-holdout and the panel publishes the GAP - the overfitting number.
Blending holdout into one "correctness" would hide exactly the number the
holdout exists to expose.
"""
import time

from app.db import get_session


_CACHE_TTL_SECONDS = 60
# PER-KEY timestamps. One shared "at" meant deriving either variant marked
# BOTH fresh: loading the admin panel refreshed the clock for a public entry
# computed up to 60s earlier, and alternating traffic could keep re-arming it
# so a stale answer was served indefinitely. Two entries need two clocks.
_cache: dict = {"public": None, "admin": None, "public_at": 0.0, "admin_at": 0.0}


def _pct(vals) -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(100.0 * sum(vals) / len(vals), 1)


def _counts(vals) -> tuple[int, int]:
    """(passed, scored) behind _pct's rate - the same filter, so the two can
    never disagree: a row with no verdict is neither passed nor scored."""
    vals = [v for v in vals if v is not None]
    return sum(vals), len(vals)


def _short_fp(fp: str | None) -> str | None:
    # 'src=26;chunks=201;sha=7603d3865a8c' -> '7603d386'
    if not fp:
        return None
    for part in fp.split(";"):
        if part.startswith("sha="):
            return part[4:][:8]
    return None


def derive_trust_panel(admin: bool = False) -> dict:
    """Compute the panel from stored eval rows. Cached briefly in-process -
    the page is public and the derivation walks every eval row."""
    now = time.time()
    key = "admin" if admin else "public"
    if _cache[key] is not None and (now - _cache[key + "_at"]) < _CACHE_TTL_SECONDS:
        return _cache[key]

    from app.models import EvalQuestion, EvalResult
    with get_session() as db:
        n_honesty_q = db.query(EvalQuestion).filter(
            EvalQuestion.category == "honesty").count()
        n_injection_q = db.query(EvalQuestion).filter(
            EvalQuestion.category == "injection").count()
        n_other_q = db.query(EvalQuestion).count() - n_honesty_q - n_injection_q
        # Materialize plain dicts INSIDE the session - ORM rows detach when it
        # closes and lazy attribute access then raises.
        rows = [{
            "run_id": r.run_id, "category": r.category, "run_at": r.run_at,
            "answer_model": r.answer_model,
            "corpus_fingerprint": r.corpus_fingerprint,
            "judge_instrument": r.judge_instrument, "run_setup": r.run_setup,
            "score": r.score,
            "faithfulness": r.faithfulness, "freshness": r.freshness,
            "retrieval_hit": r.retrieval_hit, "holdout": r.holdout,
        } for r in db.query(EvalResult).all()]

    runs: dict[str, list] = {}
    for r in rows:
        runs.setdefault(r["run_id"], []).append(r)

    complete = []
    for run_id, rr in runs.items():
        hon = [r for r in rr if r["category"] == "honesty"]
        inj = [r for r in rr if r["category"] == "injection"]
        # The injection cohort is mechanically graded against a planted
        # attack - a different rubric entirely. Like honesty, it never
        # blends into any answer-quality axis on this panel.
        rest = [r for r in rr if r["category"] not in ("honesty", "injection")]
        if len(rest) != n_other_q or n_other_q == 0:
            continue
        if hon and len(hon) != n_honesty_q:
            continue
        if inj and len(inj) != n_injection_q:
            continue
        # Tuned vs holdout split (the extension): correctness reports the
        # TUNED cohort; the holdout cohort and the gap get their own axes.
        # Where an exam has no holdout rows, tuned == rest and both extra
        # axes stay None - the peers' shape, unchanged.
        tuned = [r for r in rest if not r["holdout"]]
        hold = [r for r in rest if r["holdout"]]
        tuned_pct = _pct([r["score"] for r in tuned])
        hold_pct = _pct([r["score"] for r in hold]) if hold else None
        # The rows behind each rate ride beside it (2026-10-28), so a band can
        # say how many questions its latest run graded. Holdout counts stay
        # None where nothing is held out, like its rate.
        tuned_passed, tuned_scored = _counts([r["score"] for r in tuned])
        hold_passed, hold_scored = (_counts([r["score"] for r in hold])
                                    if hold else (None, None))
        faith_passed, faith_scored = _counts([r["faithfulness"] for r in rest])
        fresh_passed, fresh_scored = _counts([r["freshness"] for r in rest])
        # Retrieval grades the same rows; a None retrieval_hit is a question
        # that names no home document - neither a hit nor a miss, so _pct and
        # _counts both skip it. Its counts ride on the row like the others'
        # (2026-10-28): the tile below is not a band, so it serves them from
        # the latest run directly.
        hits = [r["retrieval_hit"] for r in rest]
        hit_passed, hit_scored = _counts(hits)
        complete.append({
            "run_id": run_id,
            "run_at": max((r["run_at"] or "") for r in rr),
            "writer": next((r["answer_model"] for r in rr if r["answer_model"]), None),
            "corpus": next((r["corpus_fingerprint"] for r in rr
                            if r["corpus_fingerprint"]), None),
            "instrument": next((r["judge_instrument"] for r in rr
                                if r["judge_instrument"]), None),
            "setup": next((r["run_setup"] for r in rr if r["run_setup"]), None),
            "n_rest": len(rest),
            "correctness": tuned_pct,
            "correctness_passed": tuned_passed,
            "correctness_scored": tuned_scored,
            "holdout": hold_pct,
            "holdout_passed": hold_passed,
            "holdout_scored": hold_scored,
            "gap": (round(tuned_pct - hold_pct, 1)
                    if tuned_pct is not None and hold_pct is not None else None),
            "faithfulness": _pct([r["faithfulness"] for r in rest]),
            "faithfulness_passed": faith_passed,
            "faithfulness_scored": faith_scored,
            "freshness": _pct([r["freshness"] for r in rest]),
            "freshness_passed": fresh_passed,
            "freshness_scored": fresh_scored,
            "retrieval": _pct(hits),
            "retrieval_passed": hit_passed,
            "retrieval_scored": hit_scored,
            "honesty": _pct([r["score"] for r in hon]) if hon else None,
            "honesty_n": len(hon),
        })

    if not complete:
        result = {"available": False,
                  "reason": "no complete measured runs yet"}
        _cache[key] = result
        _cache[key + "_at"] = now
        return result

    complete.sort(key=lambda c: c["run_at"])
    latest = complete[-1]
    # The judged axes read from the newest run that WAS judged (2026-11-04),
    # the way honesty reads from the newest run with honesty rows below. A
    # retrieval-only run grades nothing and stamps no instrument, so when one
    # was the latest it became the band group alone and every judged tile
    # read "not yet measured" over the judged run it had merely followed (a
    # retrieval-only A/B run after a full run is enough). Retrieval still
    # reports from the latest run alone.
    judged = [c for c in complete
              if any(c.get(a) is not None
                     for a in ("correctness", "holdout", "faithfulness", "freshness"))]
    anchor = judged[-1] if judged else latest
    # The band group: identical configuration = same writer + same corpus +
    # same JUDGE INSTRUMENT ERA + same RUN SETUP + same exam shape as the
    # anchor run. The instrument leg is load-bearing: a rubric or judge change
    # is a NEW instrument, and readings from different instruments must never
    # blend into one "band" - that spread would be instrument drift wearing a
    # noise band's clothes. The setup leg (2026-11-05, app/run_setup.py) is
    # the same rule for the system itself: build, boot, settings and the run's
    # options - two runs alike in the other four, one asking the peers and one
    # not, measured two systems. NULL-era rows never band with stamped ones.
    # Retrieval is deterministic on a still corpus, so it reports from the
    # latest run alone.
    band_runs = [c for c in complete
                 if c["writer"] == anchor["writer"]
                 and c["corpus"] == anchor["corpus"]
                 and c["instrument"] == anchor["instrument"]
                 and c["instrument"] is not None
                 and c["setup"] == anchor["setup"]
                 and c["setup"] is not None
                 and c["n_rest"] == anchor["n_rest"]]
    # Degenerate but honest fallback: if the anchor run itself is unstamped
    # (pre-stamp history), band it alone rather than with unknowns.
    if anchor["instrument"] is None or anchor["setup"] is None:
        band_runs = [anchor]

    def _band(axis: str, counted: bool = True) -> dict | None:
        scored = [c for c in band_runs if c[axis] is not None]
        if not scored:
            return None
        vals = [c[axis] for c in scored]
        band = {"low": min(vals), "high": max(vals), "runs": len(vals)}
        # The rows behind the rate (2026-10-28): the newest band run that has
        # the axis (band_runs keeps complete's run_at order) - for a judged
        # axis the latest JUDGED run's, as the band anchors there. `runs`
        # counts runs, so without these the tile never said how many
        # questions it grades. The gap takes none: it is a difference of two
        # rates, each of which carries its own.
        if counted:
            newest = scored[-1]
            band["passed"] = newest[axis + "_passed"]
            band["scored"] = newest[axis + "_scored"]
        return band

    hon_runs = [c for c in complete if c["honesty"] is not None]
    latest_hon = hon_runs[-1] if hon_runs else None

    measured_date = (latest["run_at"] or "")[:10] or None
    # The judged tiles' own date. `measured_at` is the newest run's, which is
    # the run retrieval reads; the judged tiles read the anchor, so after a
    # retrieval-only run one date named a day the judged numbers were not
    # measured on. None when no run was judged: those tiles show no number,
    # so they have no date either. Pages print both where they differ.
    judged_date = ((anchor["run_at"] or "")[:10] or None) if judged else None
    result: dict = {
        "available": True,
        "honesty": ({"pct": latest_hon["honesty"], "n": latest_hon["honesty_n"],
                     "measured_at": (latest_hon["run_at"] or "")[:10]}
                    if latest_hon else None),
        "correctness": _band("correctness"),
        "holdout": _band("holdout"),
        "gap": _band("gap", counted=False),
        "faithfulness": _band("faithfulness"),
        "freshness": _band("freshness"),
        # The latest run's rate with the rows behind it (2026-10-28): hits
        # over the questions that name a home document. None when no question
        # does - 0 of 0 is not a number to show.
        "retrieval": ({"pct": latest["retrieval"],
                       "passed": latest["retrieval_passed"],
                       "scored": latest["retrieval_scored"]}
                      if latest["retrieval"] is not None else None),
        "measured_at": measured_date,
        "judged_measured_at": judged_date,
        "corpus_fingerprint_short": _short_fp(latest["corpus"]),
        # True by construction: the same-family guard refuses a writer/judge
        # pairing from one lab unless deliberately overridden.
        "cross_family_judging": True,
    }
    if admin:
        from app.config import get_config
        result["provenance"] = {
            "writer": latest["writer"],
            "judge": get_config("eval_judge_model", ""),
            "corpus_fingerprint": latest["corpus"],
            "band_run_ids": [c["run_id"] for c in band_runs],
            "run_setup": anchor["setup"],
            "question_set": {"total": n_other_q + n_honesty_q,
                             "honesty": n_honesty_q},
        }
        # Code-level truths, stated because this build ships them - they
        # change only with the code that changes them.
        result["instrument"] = {
            "hybrid_persona_grounding": True,
            "judge_input_boundary": True,
        }
    _cache[key] = result
    _cache[key + "_at"] = now
    return result


def clear_trust_cache() -> None:
    _cache["public"] = None
    _cache["admin"] = None
    _cache["public_at"] = 0.0
    _cache["admin_at"] = 0.0
