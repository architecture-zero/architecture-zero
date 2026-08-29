"""Eval administration: the question bank, runs, results, recall.

Ninth router out of main.py. Same rules: no prefix, full literal paths, guards
verbatim on the handlers, never `from app.main import ...`.

_startup_ingest_active is read through the MODULE (runtime_config.<name>), never
from-imported. It is rebound at runtime by main's startup hooks; a from-import
would bind False once at import time and the eval-during-boot-ingest guard would
be permanently open, returning 200 on a half-embedded corpus with nothing in the
logs to say so.

_parse_retrieved lives here rather than in eval_runner: a mechanical cut would
have taken it with the engine block it sits inside, but both its callers are
routes.
"""
import os
import json
import uuid as _uuid
import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import runtime_config
from app.db import get_session
from app.eval_runner import (EVAL_SEED_PATH, _DEFAULT_EVAL_QUESTIONS,
                             sync_eval_questions_from_seed, _eval_runs,
                             _run_eval_job, _persist_rag_metric)
from app.jwt_auth import require_owner
from app.logger import log
from app.config import get_config
from app.runtime_config import (_config_or_default, DEFAULT_MODEL,
                                EVAL_JUDGE_MODEL_DEFAULT)

router = APIRouter()


class EvalQuestionIn(BaseModel):
    question: str
    category: str = "general"
    notes: str = ""
    expected_source: str | None = None
    as_level: int | None = None  # clearance the question is asked at; None = Owner
    holdout: int | None = None   # 1 = locked-holdout cohort; 0/None = tuned


class EvalQuestionUpdate(BaseModel):
    question: str | None = None
    category: str | None = None
    notes: str | None = None
    expected_source: str | None = None
    as_level: int | None = None
    holdout: int | None = None


class EvalRunRequest(BaseModel):
    question_ids: list[int] = []  # empty = run all
    model: str = ""               # empty = eval_answer_model config, then DEFAULT_MODEL
    use_rag: bool = True
    n_results: int = 5            # kept after rerank - mirrors the chat's RERANK_TOP_K
    retrieval_only: bool = True   # recall needs no LLM answer; skip it (avoids 504 on big runs)
    # Deliberate same-family override: a run whose answer model and judge
    # share a provider family is refused unless this is explicitly true -
    # self-graded scores must never happen by accident.
    allow_same_family: bool = False


class EvalScoreUpdate(BaseModel):
    score: int | None = None  # 1=pass, 0=fail, None=clear score
@router.get("/api/admin/evals/questions")
def list_eval_questions(category: str | None = None, current_user: dict = Depends(require_owner)):
    from app.models import EvalQuestion
    with get_session() as db:
        q = db.query(EvalQuestion)
        if category:
            q = q.filter(EvalQuestion.category == category)
        rows = q.order_by(EvalQuestion.category, EvalQuestion.id).all()
        return {"questions": [
            {"id": r.id, "question": r.question, "category": r.category,
             "notes": r.notes or "", "expected_source": r.expected_source,
             "as_level": r.as_level, "holdout": r.holdout or 0,
             "created_at": r.created_at}
            for r in rows
        ]}


@router.post("/api/admin/evals/questions/seed")
def seed_eval_questions(current_user: dict = Depends(require_owner)):
    from app.models import EvalQuestion

    # Load from instance-specific file if configured, otherwise use generic
    # defaults
    if EVAL_SEED_PATH and os.path.exists(EVAL_SEED_PATH):
        with open(EVAL_SEED_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        questions = [(q["category"], q["question"], q.get("notes", ""), q.get("expected_source"),
                      q.get("as_level"), 1 if q.get("holdout") else 0) for q in raw]
    else:
        questions = [(c, q, n, None, None, 0) for (c, q, n) in _DEFAULT_EVAL_QUESTIONS]

    with get_session() as db:
        existing = db.query(EvalQuestion).count()
        if existing > 0:
            raise HTTPException(status_code=409, detail=f"Already have {existing} questions. Delete all first.")
        now = _dt.datetime.utcnow().isoformat()
        for category, question, notes, expected_source, as_level, holdout in questions:
            db.add(EvalQuestion(question=question, category=category, notes=notes,
                                expected_source=expected_source, as_level=as_level,
                                holdout=holdout, created_at=now))
    return {"seeded": len(questions)}
@router.post("/api/admin/evals/questions/sync")
def sync_eval_questions(current_user: dict = Depends(require_owner)):
    """On-demand reconcile of the DB question set from the seed file."""
    return sync_eval_questions_from_seed()


@router.post("/api/admin/evals/questions")
def create_eval_question(body: EvalQuestionIn, current_user: dict = Depends(require_owner)):
    from app.models import EvalQuestion
    with get_session() as db:
        row = EvalQuestion(
            question=body.question.strip(),
            category=body.category.strip() or "general",
            notes=body.notes.strip(),
            expected_source=(body.expected_source or None),
            as_level=body.as_level,
            holdout=(1 if body.holdout else 0),
            created_at=_dt.datetime.utcnow().isoformat(),
        )
        db.add(row)
        db.flush()
        return {"id": row.id, "question": row.question, "category": row.category,
                "notes": row.notes or "", "expected_source": row.expected_source,
                "as_level": row.as_level, "holdout": row.holdout or 0,
                "created_at": row.created_at}


@router.patch("/api/admin/evals/questions/{question_id}")
def update_eval_question(question_id: int, body: EvalQuestionUpdate,
                         current_user: dict = Depends(require_owner)):
    from app.models import EvalQuestion
    with get_session() as db:
        row = db.query(EvalQuestion).filter(EvalQuestion.id == question_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Question not found")
        if body.question is not None:
            row.question = body.question.strip()
        if body.category is not None:
            row.category = body.category.strip() or "general"
        if body.notes is not None:
            row.notes = body.notes.strip()
        if body.expected_source is not None:
            row.expected_source = body.expected_source.strip() or None
        if body.as_level is not None:
            row.as_level = body.as_level
        if body.holdout is not None:
            row.holdout = 1 if body.holdout else 0
        return {"id": row.id, "question": row.question, "category": row.category,
                "notes": row.notes or "", "expected_source": row.expected_source,
                "as_level": row.as_level, "holdout": row.holdout or 0}


@router.delete("/api/admin/evals/questions/{question_id}")
def delete_eval_question(question_id: int, current_user: dict = Depends(require_owner)):
    from app.models import EvalQuestion
    with get_session() as db:
        row = db.query(EvalQuestion).filter(EvalQuestion.id == question_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Question not found")
        db.delete(row)
    return {"ok": True}


@router.delete("/api/admin/evals/questions")
def delete_all_eval_questions(current_user: dict = Depends(require_owner)):
    from app.models import EvalQuestion
    with get_session() as db:
        count = db.query(EvalQuestion).delete()
    return {"deleted": count}
def _parse_retrieved(raw: str | None) -> list[dict]:
    """Normalize stored retrieved_sources to [{source, score}]. Tolerates the
    old format (a plain list of source-name strings)."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return [{"source": x.get("source"), "score": x.get("score")} if isinstance(x, dict)
            else {"source": x, "score": None} for x in data]
@router.post("/api/admin/evals/run")
def run_evals(body: EvalRunRequest, current_user: dict = Depends(require_owner)):
    """Kick off a background eval run and return immediately (poll
    run-status). A synchronous run of a large set embeds + reranks per
    question on CPU and 504s."""
    from app.models import EvalQuestion
    import threading
    if runtime_config._startup_ingest_active:
        raise HTTPException(
            status_code=409,
            detail="Startup ingest is still re-embedding the corpus - an eval now would "
                   "measure a half-migrated index. Retry after both startup_sync_done "
                   "lines appear in the backend logs.")
    run_id = str(_uuid.uuid4())
    run_at = _dt.datetime.utcnow().isoformat()
    # Answer-model resolution: the eval writer is PINNED via config
    # (eval_answer_model), independent of the admin chat dial - same
    # principle as the pinned judge: a measurement instrument must not change
    # because the chat model did. Explicit per-run model still wins;
    # DEFAULT_MODEL is the last resort for a box with neither config set.
    model = (body.model.strip() or get_config("eval_answer_model", "")
             or _config_or_default("default_model", DEFAULT_MODEL))
    if not body.retrieval_only:
        # Same-family guard: the judge must not grade its own lab's writer -
        # self-preference bias puts a thumb on every score, and the collision
        # arrives silently via the admin dial. Provider head == lab for cloud
        # models; local Ollama hosts many model families but never judges, so
        # the comparison stays honest. Retrieval-only runs generate and judge
        # nothing, so they are exempt.
        from app.providers import _provider_for_model
        judge_model = _config_or_default("eval_judge_model", EVAL_JUDGE_MODEL_DEFAULT)
        if (_provider_for_model(model) == _provider_for_model(judge_model)
                and not body.allow_same_family):
            raise HTTPException(
                status_code=400,
                detail=f"Answer model '{model}' and judge model '{judge_model}' are "
                       f"the same provider family ('{_provider_for_model(model)}') - "
                       "the scores would be self-graded. Change eval_answer_model or "
                       "eval_judge_model in admin config, or pass "
                       "allow_same_family=true to override deliberately.")
        if (_provider_for_model(model) == _provider_for_model(judge_model)
                and body.allow_same_family):
            log("eval_same_family_override", run_id=run_id, model=model,
                judge_model=judge_model)
    with get_session() as db:
        if body.question_ids:
            rows = db.query(EvalQuestion).filter(EvalQuestion.id.in_(body.question_ids)).all()
        else:
            rows = db.query(EvalQuestion).order_by(EvalQuestion.category, EvalQuestion.id).all()
        if not rows:
            raise HTTPException(status_code=400, detail="No questions to run")
        questions = [{"id": r.id, "question": r.question, "category": r.category,
                      "expected_source": r.expected_source, "notes": r.notes,
                      "as_level": r.as_level, "holdout": r.holdout or 0,
                      "setup_turns": r.setup_turns} for r in rows]
    _eval_runs[run_id] = {"total": len(questions), "done": 0, "complete": False, "run_at": run_at}
    threading.Thread(
        target=_run_eval_job,
        args=(run_id, run_at, questions, model, body.use_rag, body.n_results, body.retrieval_only),
        daemon=True,
    ).start()
    return {"run_id": run_id, "run_at": run_at, "total": len(questions), "status": "running"}


@router.get("/api/admin/evals/run-status/{run_id}")
def eval_run_status(run_id: str, current_user: dict = Depends(require_owner)):
    st = _eval_runs.get(run_id, {})
    # failed/error are REPORTED, not merely recorded. The runner sets complete
    # True from a finally precisely so a crashed run stops looking like it is
    # still going - but this handler returned only total/done/complete, so a run
    # that died mid-loop came back complete=True and read as a normal finish.
    # The caller then went looking for results that were never written, with the
    # crash reason sitting unreported in the same dict. `failed` is what tells a
    # finished run from a dead one; without it, complete=True is exactly as
    # misleading as the indefinite "running" it replaced.
    return {"run_id": run_id, "total": st.get("total"), "done": st.get("done", 0),
            "complete": st.get("complete", False),
            "failed": st.get("failed", False), "error": st.get("error")}


@router.get("/api/admin/evals/runs")
def list_eval_runs(current_user: dict = Depends(require_owner)):
    from app.models import EvalResult
    with get_session() as db:
        rows = db.query(EvalResult).order_by(EvalResult.run_at.desc()).all()
        data = [{"run_id": r.run_id, "run_at": r.run_at, "score": r.score,
                 "faithfulness": r.faithfulness, "freshness": r.freshness,
                 "holdout": r.holdout, "category": r.category,
                 "retrieval_hit": r.retrieval_hit}
                for r in rows]

    runs: dict[str, dict] = {}
    for r in data:
        if r["run_id"] not in runs:
            runs[r["run_id"]] = {"run_id": r["run_id"], "run_at": r["run_at"], "total": 0,
                                 "scored": 0, "passed": 0,
                                 "faith_scored": 0, "faith_passed": 0,
                                 "fresh_scored": 0, "fresh_passed": 0,
                                 "holdout_scored": 0, "holdout_passed": 0,
                                 "honesty_scored": 0, "honesty_passed": 0,
                                 "injection_total": 0, "injection_reached": 0,
                                 "injection_scored": 0, "injection_passed": 0}
        run = runs[r["run_id"]]
        run["total"] += 1
        # The honesty cohort reports ONLY as its own refuse-vs-fabricate
        # aggregate: its verdicts grade a different rubric, so blending them
        # into ANY tuned headline (answers, faith, fresh) would break
        # like-for-like with history.
        if r["category"] == "honesty":
            if r["score"] is not None:
                run["honesty_scored"] += 1
                if r["score"] == 1:
                    run["honesty_passed"] += 1
            continue
        # Injection cohort: mechanical refuse-the-poison verdicts over a
        # transiently planted hostile doc - own aggregate, never blended (the
        # honesty rule); `reached` counts rows where the poison actually hit
        # the assembled context (retrieval_hit on the fixture), so a vacuous
        # pass (poison never retrieved) is visible, not flattering.
        if r["category"] == "injection":
            run["injection_total"] += 1
            if r["retrieval_hit"] == 1:
                run["injection_reached"] += 1
            if r["score"] is not None:
                run["injection_scored"] += 1
                if r["score"] == 1:
                    run["injection_passed"] += 1
            continue
        # Holdout rows report ONLY as their own aggregate - blended into the
        # tuned headline they'd hide exactly the gap they exist to measure.
        # Null holdout = older history = tuned.
        if r["holdout"]:
            if r["score"] is not None:
                run["holdout_scored"] += 1
                if r["score"] == 1:
                    run["holdout_passed"] += 1
            continue
        if r["score"] is not None:
            run["scored"] += 1
            if r["score"] == 1:
                run["passed"] += 1
        if r["faithfulness"] is not None:
            run["faith_scored"] += 1
            if r["faithfulness"] == 1:
                run["faith_passed"] += 1
        if r["freshness"] is not None:
            run["fresh_scored"] += 1
            if r["freshness"] == 1:
                run["fresh_passed"] += 1

    # Answer-quality headline per run (passed/scored, TUNED set) - plus
    # faithfulness (correct AND grounded) and freshness (grounded on CURRENT
    # material): the three legs of the confusion matrix. The overfit check
    # rides along: holdout_pct over the never-tuned-against cohort, and gap =
    # tuned minus holdout - the headline is three numbers, never one blended
    # %.
    for run in runs.values():
        run["answer_pct"] = (round(100 * run["passed"] / run["scored"], 1)
                             if run["scored"] else None)
        run["faithful_pct"] = (round(100 * run["faith_passed"] / run["faith_scored"], 1)
                               if run["faith_scored"] else None)
        run["fresh_pct"] = (round(100 * run["fresh_passed"] / run["fresh_scored"], 1)
                            if run["fresh_scored"] else None)
        run["holdout_pct"] = (round(100 * run["holdout_passed"] / run["holdout_scored"], 1)
                              if run["holdout_scored"] else None)
        run["gap"] = (round(run["answer_pct"] - run["holdout_pct"], 1)
                      if run["answer_pct"] is not None and run["holdout_pct"] is not None
                      else None)
        run["honesty_pct"] = (round(100 * run["honesty_passed"] / run["honesty_scored"], 1)
                              if run["honesty_scored"] else None)
        run["injection_pct"] = (round(100 * run["injection_passed"] / run["injection_scored"], 1)
                                if run["injection_scored"] else None)

    return {"runs": sorted(runs.values(), key=lambda x: x["run_at"], reverse=True)}


@router.get("/api/admin/evals/runs/{run_id}")
def get_eval_run(run_id: str, current_user: dict = Depends(require_owner)):
    from app.models import EvalResult
    with get_session() as db:
        rows = db.query(EvalResult).filter(EvalResult.run_id == run_id).order_by(EvalResult.id).all()
        if not rows:
            raise HTTPException(status_code=404, detail="Run not found")

        def _row(r):
            # Holdout structural lock: a holdout row shows THAT it passed or
            # failed (the aggregate needs it) but never WHY or WHERE - the
            # response, rationales, rank, and retrieved sources are the
            # miss-diagnosis material a fix would be tuned from. Storage
            # stays complete; the surface withholds. Procedural, not
            # cryptographic.
            hold = bool(r.holdout)
            return {
                "id": r.id, "question_id": r.question_id, "question_text": r.question_text,
                "category": r.category, "holdout": 1 if hold else 0,
                "response": "[holdout - diagnostics withheld]" if hold else r.response,
                "score": r.score,
                "judge_rationale": None if hold else r.judge_rationale,
                "faithfulness": r.faithfulness,
                "faithfulness_rationale": None if hold else r.faithfulness_rationale,
                "freshness": r.freshness,
                "freshness_rationale": None if hold else r.freshness_rationale,
                "retrieval_hit": r.retrieval_hit,
                "retrieval_rank": None if hold else r.retrieval_rank,
                "retrieved_sources": [] if hold else _parse_retrieved(r.retrieved_sources),
                "run_at": r.run_at}

        return {
            "run_id": run_id, "run_at": rows[0].run_at,
            "results": [_row(r) for r in rows],
        }


@router.patch("/api/admin/evals/results/{result_id}")
def score_eval_result(result_id: int, body: EvalScoreUpdate, current_user: dict = Depends(require_owner)):
    from app.models import EvalResult
    with get_session() as db:
        row = db.query(EvalResult).filter(EvalResult.id == result_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Result not found")
        row.score = body.score
        return {"id": row.id, "score": row.score}
@router.get("/api/admin/evals/recall")
def eval_recall(run_id: str | None = None, current_user: dict = Depends(require_owner)):
    """Retrieval-recall summary + the Knowledge Gaps list for a run (default:
    latest).

    Recall = of the questions that have an expected_source, how many had that
    source actually retrieved. Gaps = the misses (fact is in the corpus but
    retrieval didn't surface it). Guardrail questions (no expected_source)
    are reported separately: they must be answer-reviewed, not
    recall-scored.
    """
    from app.models import EvalResult, EvalQuestion
    with get_session() as db:
        if not run_id:
            latest = db.query(EvalResult).order_by(EvalResult.run_at.desc()).first()
            if not latest:
                return {"run_id": None, "recall": None, "gaps": [], "guardrail": []}
            run_id = latest.run_id
        rows = db.query(EvalResult).filter(EvalResult.run_id == run_id).order_by(EvalResult.id).all()
        if not rows:
            raise HTTPException(status_code=404, detail="Run not found")
        # expected_source lives on the question - join by question_id
        qids = [r.question_id for r in rows if r.question_id]
        q_src = {q.id: q.expected_source
                 for q in db.query(EvalQuestion).filter(EvalQuestion.id.in_(qids)).all()} if qids else {}

        # Holdout structural lock: holdout rows never enter the tuned recall
        # number, the Gaps list, or the guardrail review list - Gaps is the
        # fix-feeding surface, and a fix aimed at a holdout miss un-locks the
        # holdout. They surface ONLY as the aggregate below.
        tuned = [r for r in rows if not r.holdout]
        hold_rows = [r for r in rows if r.holdout]
        # Injection rows carry expected_source = the planted fixture, so
        # their retrieval_hit means "the poison reached context" - a
        # cohort-internal signal. They are excluded from the corpus recall
        # number and from Gaps (the fix-feeding surface): a "fix" aimed at
        # retrieving poison better would be tuning the system toward the
        # attack.
        scored = [r for r in tuned
                  if r.retrieval_hit is not None
                  and r.category != "injection"]
        hits = sum(1 for r in scored if r.retrieval_hit == 1)
        total = len(scored)
        gaps, guardrail, honesty, injection = [], [], [], []
        for r in tuned:
            exp = q_src.get(r.question_id)
            item = {
                "question_id": r.question_id, "question_text": r.question_text,
                "category": r.category, "expected_source": exp,
                "retrieval_hit": r.retrieval_hit, "retrieval_rank": r.retrieval_rank,
                "retrieved_sources": _parse_retrieved(r.retrieved_sources),
                "response": r.response, "score": r.score,
                "judge_rationale": r.judge_rationale,
            }
            if r.category == "injection":
                # Diagnosable by design, like honesty - improving the
                # BEHAVIOR is the point; the questions still never change
                # once seeded.
                injection.append(item)
            elif r.retrieval_hit == 0:
                gaps.append(item)
            elif r.category == "honesty":
                # Refuse-vs-fabricate rows are diagnosable by design (no
                # holdout-style lock - improving the BEHAVIOR is the point;
                # the questions still never change once seeded).
                honesty.append(item)
            elif exp is None:
                guardrail.append(item)
        recall = {"hits": hits, "total": total,
                  "pct": round(100 * hits / total) if total else None}
        h_scored = [r for r in hold_rows if r.retrieval_hit is not None]
        h_hits = sum(1 for r in h_scored if r.retrieval_hit == 1)
        holdout_recall = ({"hits": h_hits, "total": len(h_scored),
                           "pct": round(100 * h_hits / len(h_scored))}
                          if h_scored else None)
        _persist_rag_metric(run_id, rows[0].run_at, recall)
        return {
            "run_id": run_id, "run_at": rows[0].run_at,
            "recall": recall, "holdout_recall": holdout_recall,
            "gaps": gaps, "guardrail": guardrail, "honesty": honesty,
            "injection": injection,
        }
