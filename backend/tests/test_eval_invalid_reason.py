"""A broken or unfinished eval run says so in the runs list (2026-10-02).

A run whose answers all errored read as a 0% system, and a run that died
partway read as a complete result over the rows it had - the list carried no
mark for either, so a caller reading it after POST /api/admin/evals/run took
both for measurements. Each run now carries `invalid_reason`; a run that is
still going is short too, and says "still running", not "did not finish".
Every case but the clean one fails on the code before this change.
"""
import uuid

from app.db import get_session
from app.eval_runner import _eval_runs
from app.models import EvalResult


def _rows(run_id, n, errored=0):
    with get_session() as db:
        for i in range(n):
            db.add(EvalResult(run_id=run_id, question_text=f"q{i}", category="general",
                              response="[ERROR: provider unreachable]" if i < errored else "an answer",
                              score=0 if i < errored else 1, run_at="2026-10-02T00:00:00"))


def _run(client, headers, run_id):
    runs = client.get("/api/admin/evals/runs", headers=headers).json()["runs"]
    return next(r for r in runs if r["run_id"] == run_id)


def _case(client, headers, rows, errored=0, registry=None):
    run_id = f"invalid-reason-{uuid.uuid4().hex[:8]}"
    if registry is not None:
        _eval_runs[run_id] = registry
    try:
        _rows(run_id, rows, errored)
        return _run(client, headers, run_id)
    finally:
        _eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()


def test_a_run_whose_answers_all_errored_is_marked_broken(client, admin_headers):
    run = _case(client, admin_headers, 3, errored=3)
    assert run["errored"] == 3
    assert run["invalid_reason"] == "every answer errored - check the provider/model config"


def test_a_run_that_died_partway_is_marked_incomplete(client, admin_headers):
    run = _case(client, admin_headers, 3, registry={"total": 5, "done": 3, "complete": True,
                                                    "failed": True, "error": "boom"})
    assert run["invalid_reason"] == ("incomplete run - 3 of 5 questions recorded; "
                                     "the run did not finish")


def test_a_run_still_going_says_so(client, admin_headers):
    run = _case(client, admin_headers, 2, registry={"total": 5, "done": 2, "complete": False})
    assert run["invalid_reason"] == "incomplete run - 2 of 5 questions recorded; still running"


def test_a_clean_run_carries_no_mark(client, admin_headers):
    run = _case(client, admin_headers, 4, errored=1, registry={"total": 4, "done": 4,
                                                               "complete": True})
    assert run["errored"] == 1
    assert run["invalid_reason"] is None
