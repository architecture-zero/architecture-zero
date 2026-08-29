"""Public trust panel - every number derived LIVE from eval_results.

Design rules:

- A public panel shows THIS instance's own per-corpus numbers, with
  provenance, and shows BANDS (spread across identical-config runs), never a
  single lucky point.
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
            "judge_instrument": r.judge_instrument, "score": r.score,
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
        complete.append({
            "run_id": run_id,
            "run_at": max((r["run_at"] or "") for r in rr),
            "writer": next((r["answer_model"] for r in rr if r["answer_model"]), None),
            "corpus": next((r["corpus_fingerprint"] for r in rr
                            if r["corpus_fingerprint"]), None),
            "instrument": next((r["judge_instrument"] for r in rr
                                if r["judge_instrument"]), None),
            "n_rest": len(rest),
            "correctness": tuned_pct,
            "holdout": hold_pct,
            "gap": (round(tuned_pct - hold_pct, 1)
                    if tuned_pct is not None and hold_pct is not None else None),
            "faithfulness": _pct([r["faithfulness"] for r in rest]),
            "freshness": _pct([r["freshness"] for r in rest]),
            "retrieval": _pct([r["retrieval_hit"] for r in rest
                               if r["retrieval_hit"] is not None]),
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
    # The band group: identical configuration = same writer + same corpus +
    # same JUDGE INSTRUMENT ERA + same exam shape as the latest run. The
    # instrument leg is load-bearing: a rubric or judge change is a NEW
    # instrument, and readings from different instruments must never blend
    # into one "band" - that spread would be instrument drift wearing a
    # noise band's clothes. NULL-era rows never band with stamped ones.
    # Retrieval is deterministic on a still corpus, so it reports from the
    # latest run alone.
    band_runs = [c for c in complete
                 if c["writer"] == latest["writer"]
                 and c["corpus"] == latest["corpus"]
                 and c["instrument"] == latest["instrument"]
                 and c["instrument"] is not None
                 and c["n_rest"] == latest["n_rest"]]
    # Degenerate but honest fallback: if the latest run itself is unstamped
    # (pre-stamp history only), band it alone rather than with unknowns.
    if latest["instrument"] is None:
        band_runs = [latest]

    def _band(axis: str) -> dict | None:
        vals = [c[axis] for c in band_runs if c[axis] is not None]
        if not vals:
            return None
        return {"low": min(vals), "high": max(vals), "runs": len(vals)}

    hon_runs = [c for c in complete if c["honesty"] is not None]
    latest_hon = hon_runs[-1] if hon_runs else None

    measured_date = (latest["run_at"] or "")[:10] or None
    result: dict = {
        "available": True,
        "honesty": ({"pct": latest_hon["honesty"], "n": latest_hon["honesty_n"],
                     "measured_at": (latest_hon["run_at"] or "")[:10]}
                    if latest_hon else None),
        "correctness": _band("correctness"),
        "holdout": _band("holdout"),
        "gap": _band("gap"),
        "faithfulness": _band("faithfulness"),
        "freshness": _band("freshness"),
        "retrieval": ({"pct": latest["retrieval"]}
                      if latest["retrieval"] is not None else None),
        "measured_at": measured_date,
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
