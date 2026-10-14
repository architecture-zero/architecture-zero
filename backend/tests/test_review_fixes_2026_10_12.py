"""Five small fixes from the 2026-10-12 outside review, each pinned here:
the audit CSV's formula cells and its streaming export, BM25 on candidates
with no word characters, the judge parser's greedy match, and a question
listed twice in the eval seed file.

The database tests run against a fresh in-memory database patched into the
module under test, so nothing here reads or leaves rows in the shared one."""
import csv
import io
import json
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers every table on Base
from app.db import Base


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


# -- The audit CSV export -------------------------------------------------------

def _audit_rows(audit, prompts):
    for i, prompt in enumerate(prompts):
        audit.log_audit_entry(user_id=1, username="csvprobe", session_id=f"s{i}",
                              prompt=prompt, response_length=3, model="m",
                              use_rag=False, sources=[])


def _export(audit, **kw):
    out = audit.export_audit_csv(**kw)
    assert not isinstance(out, str), "the export streams; it never builds one string"
    return list(csv.DictReader(io.StringIO("".join(out))))


def test_audit_csv_neutralizes_formula_cells(monkeypatch, fresh_session):
    import app.audit as audit
    monkeypatch.setattr(audit, "get_session", fresh_session)
    prompts = ["=1+2", "+SUM(A1)", "-2+3", "@cmd", "\tlead tab", "\rlead cr",
               "plain question", "a = b in the middle"]
    _audit_rows(audit, prompts)
    got = [r["prompt_preview"] for r in _export(audit)]
    assert got == ["'=1+2", "'+SUM(A1)", "'-2+3", "'@cmd", "'\tlead tab",
                   "'\rlead cr", "plain question", "a = b in the middle"]


def test_audit_csv_numbers_are_untouched(monkeypatch, fresh_session):
    import app.audit as audit
    monkeypatch.setattr(audit, "get_session", fresh_session)
    _audit_rows(audit, ["q"])
    row = _export(audit)[0]
    assert row["response_length"] == "3" and row["user_id"] == "1"


@pytest.mark.parametrize("batch", [2, 5])
def test_audit_csv_streams_in_batches(monkeypatch, fresh_session, batch):
    """Five rows read two at a time (a short last batch) and five at a time (an
    exact multiple, so one empty read ends it): every row once, in id order."""
    import app.audit as audit
    monkeypatch.setattr(audit, "get_session", fresh_session)
    monkeypatch.setattr(audit, "EXPORT_BATCH_ROWS", batch)
    _audit_rows(audit, [f"q{i}" for i in range(5)])
    rows = _export(audit)
    assert [r["prompt_preview"] for r in rows] == [f"q{i}" for i in range(5)]
    ids = [int(r["id"]) for r in rows]
    assert ids == sorted(ids)


def test_audit_csv_filters_still_apply(monkeypatch, fresh_session):
    import app.audit as audit
    monkeypatch.setattr(audit, "get_session", fresh_session)
    monkeypatch.setattr(audit, "EXPORT_BATCH_ROWS", 1)
    _audit_rows(audit, ["mine"])
    audit.log_audit_entry(user_id=2, username="someone-else", session_id="x",
                          prompt="theirs", response_length=1, model="m",
                          use_rag=False, sources=[])
    assert [r["prompt_preview"] for r in _export(audit, username_filter="csvprobe")] == ["mine"]
    assert _export(audit, date_to="2000-01-01") == []


def test_audit_export_route_streams_csv(client, admin_headers):
    r = client.get("/api/admin/audit/export", headers=admin_headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=audit_log.csv" in r.headers["content-disposition"]
    assert r.text.splitlines()[0].startswith("id,timestamp,username")


# -- BM25 with no word characters ------------------------------------------------

def test_bm25_scores_punctuation_only_candidates():
    from app.database import _bm25_scores
    assert _bm25_scores("deploy steps", ["!!!", "...", ""]) == [0.0, 0.0, 0.0]
    assert _bm25_scores("deploy steps", []) == []


# -- The judge parser ------------------------------------------------------------

def test_parse_verdict_with_a_brace_after_it():
    from app.eval_judge import _parse_verdict
    assert _parse_verdict('{"pass": true, "rationale": "ok"}\nNote: see {x}.') == \
        {"pass": True, "rationale": "ok"}
    assert _parse_verdict('Aside {not json} then {"pass": false, "rationale": "r"}') == \
        {"pass": False, "rationale": "r"}
    # Braces inside the rationale string are part of the object, not its end.
    assert _parse_verdict('{"pass": false, "rationale": "says {x} wrongly"}') == \
        {"pass": False, "rationale": "says {x} wrongly"}
    # The first object WITH a bool pass wins; an earlier object without one
    # is stepped past.
    assert _parse_verdict('{"note": 1} {"pass": true, "rationale": ""}') == \
        {"pass": True, "rationale": ""}
    assert _parse_verdict('{x} {y}') is None


# -- A question listed twice in the seed file ------------------------------------

def _seed_file(tmp_path, questions):
    p = tmp_path / "eval-seed.json"
    p.write_text(json.dumps(questions), encoding="utf-8")
    return str(p)


_TWICE = [
    {"category": "a", "question": "Q repeated?", "notes": "first"},
    {"category": "b", "question": "Q once?"},
    {"category": "c", "question": "Q repeated?", "notes": "second"},
]


def test_seed_sync_adds_a_repeated_question_once(monkeypatch, fresh_session, tmp_path):
    import app.eval_runner as runner
    from app.models import EvalQuestion
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", _seed_file(tmp_path, _TWICE))
    result = runner.sync_eval_questions_from_seed()
    assert result["added"] == 2
    assert result["repeated"] == ["Q repeated?"]
    with fresh_session() as db:
        rows = db.query(EvalQuestion).all()
        assert sorted(r.question for r in rows) == ["Q once?", "Q repeated?"]
        # The first entry is the one synced.
        assert next(r for r in rows if r.question == "Q repeated?").notes == "first"
    # A second sync of the same file changes nothing and still names the repeat.
    again = runner.sync_eval_questions_from_seed()
    assert (again["added"], again["updated"], again["unchanged"]) == (0, 0, 2)
    assert again["repeated"] == ["Q repeated?"]


def test_seed_route_adds_a_repeated_question_once(monkeypatch, fresh_session, tmp_path):
    import app.routers.evals as evals
    from app.models import EvalQuestion
    monkeypatch.setattr(evals, "get_session", fresh_session)
    monkeypatch.setattr(evals, "EVAL_SEED_PATH", _seed_file(tmp_path, _TWICE))
    assert evals.seed_eval_questions(current_user={}) == {"seeded": 2}
    with fresh_session() as db:
        assert sorted(r.question for r in db.query(EvalQuestion).all()) == \
            ["Q once?", "Q repeated?"]
