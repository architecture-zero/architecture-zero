"""The last of the seed path: an unreadable seed file is refused like a bad
one, and the seed route reads its rows by the sync's own rules.

1. eval_runner._read_seed_file caught a file that is not JSON or not a list,
   but an error at open() - permissions, a directory at the path - still
   raised: a warning at boot and a 500 from the routes. It is now refused
   with its rule like the others: one error line naming the rule (never the
   error's own words), nothing written, a 400 from the sync and seed routes.
2. The seed route read each row as q["category"] and q["question"], so a row
   that was not an object, or had no category, raised (a 500). It now reads
   the rows through eval_runner._seed_rows - the sync's own checks
   (_seed_question, _seed_row_problem) and the sync's own reading of each
   field (_seed_values). A row the sync would refuse is named in the answer
   ("refused": its place, its text or null, the rule) and skipped, with an
   eval_seed_row_refused error line. A row with no category is not a
   refusal - the sync has always read it as "general", and so does the seed
   route now. A kept row is stored as the sync stores it, so the next sync
   finds nothing to change. A clean file answers as before.

FIX cases fail on the code before this change; CONTROL cases pass on both.
"""
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registers every table on Base
from app.db import Base

BACKEND = Path(__file__).resolve().parents[1]
SEED_FILE = BACKEND / "eval-questions.json"
UNREADABLE = ("the seed file cannot be read: it must be a readable file holding a JSON "
              "list of questions")
ROW_RULE = "a seed row must be a JSON object"


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


@pytest.fixture
def lines(monkeypatch):
    import app.eval_runner as runner
    said: list = []
    monkeypatch.setattr(runner, "log_error", lambda event, **f: said.append((event, f)))
    return said


def _seed_file(tmp_path, rows):
    path = tmp_path / "eval-seed.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def _stored(fresh_session):
    from app.models import EvalQuestion
    with fresh_session() as db:
        return {r.question: (r.category, r.notes) for r in db.query(EvalQuestion).all()}


# -- 1. a seed file that cannot be read -----------------------------------------------

def test_fix_a_directory_at_the_path_is_refused_by_the_sync(monkeypatch, fresh_session,
                                                           tmp_path, lines):
    import app.eval_runner as runner
    (tmp_path / "seed-dir").mkdir()
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", str(tmp_path / "seed-dir"))
    assert runner.sync_eval_questions_from_seed() == {"status": "refused", "rule": UNREADABLE}
    assert lines == [("eval_seed_file_refused", {"rule": UNREADABLE})]
    assert _stored(fresh_session) == {}


def test_fix_an_error_at_open_is_refused_and_its_words_are_not_said(monkeypatch, fresh_session,
                                                                   tmp_path, lines):
    import app.eval_runner as runner

    def _denied(*a, **k):
        raise PermissionError(13, "Permission denied: Zq7-error-words")
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", _seed_file(tmp_path, [{"question": "Q?"}]))
    monkeypatch.setattr(runner, "open", _denied, raising=False)
    result = runner.sync_eval_questions_from_seed()
    assert result == {"status": "refused", "rule": UNREADABLE}
    assert "Zq7-error-words" not in json.dumps(result) + json.dumps(lines)
    assert _stored(fresh_session) == {}


@pytest.mark.parametrize("route", ["sync", "seed"])
def test_fix_both_routes_answer_400_for_an_unreadable_file(client, admin_headers, monkeypatch,
                                                          fresh_session, tmp_path, route):
    import app.eval_runner as runner
    import app.routers.evals as evals
    (tmp_path / "seed-dir").mkdir()
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", str(tmp_path / "seed-dir"))
    monkeypatch.setattr(evals, "EVAL_SEED_PATH", str(tmp_path / "seed-dir"))
    r = client.post(f"/api/admin/evals/questions/{route}", headers=admin_headers)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == UNREADABLE


# -- 2. the seed route reads its rows by the sync's rules -----------------------------

def _seed(monkeypatch, fresh_session, tmp_path, rows):
    import app.routers.evals as evals
    monkeypatch.setattr(evals, "get_session", fresh_session)
    monkeypatch.setattr(evals, "EVAL_SEED_PATH", _seed_file(tmp_path, rows))
    return evals.seed_eval_questions(current_user={})


def test_fix_a_row_the_sync_would_refuse_is_named_and_skipped(monkeypatch, fresh_session,
                                                             tmp_path, lines):
    rows = [{"category": "a", "question": "Q one?"},
            "a stray string",
            {"category": 5, "question": "Q bad category?"},
            {"category": "b", "question": "   "},
            {"category": "c", "question": "Q two?"}]
    answer = _seed(monkeypatch, fresh_session, tmp_path, rows)
    assert answer["seeded"] == 2
    assert [(x["index"], x["question"], x["rule"]) for x in answer["refused"]] == [
        (1, None, ROW_RULE),
        (2, "Q bad category?", "category must be text"),
        (3, None, answer["refused"][2]["rule"])]
    assert answer["refused"][2]["rule"].startswith("empty question")
    assert [e for e, _ in lines] == ["eval_seed_row_refused"] * 3
    assert sorted(_stored(fresh_session)) == ["Q one?", "Q two?"]


def test_fix_a_row_with_no_category_is_seeded_as_the_sync_reads_it(monkeypatch, fresh_session,
                                                                  tmp_path):
    answer = _seed(monkeypatch, fresh_session, tmp_path, [{"question": "Q no category?"}])
    assert answer == {"seeded": 1}
    assert _stored(fresh_session) == {"Q no category?": ("general", "")}


def test_fix_a_seeded_row_is_one_the_next_sync_leaves_alone(monkeypatch, fresh_session,
                                                           tmp_path):
    import app.eval_runner as runner
    rows = [{"category": " padded ", "question": "Q padded?", "notes": "  said  "}]
    assert _seed(monkeypatch, fresh_session, tmp_path, rows) == {"seeded": 1}
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", _seed_file(tmp_path, rows))
    result = runner.sync_eval_questions_from_seed()
    assert (result["added"], result["updated"], result["unchanged"]) == (0, 0, 1)
    assert _stored(fresh_session) == {"Q padded?": ("padded", "said")}


def test_control_a_clean_file_seeds_as_before(monkeypatch, fresh_session, tmp_path, lines):
    rows = [{"category": "a", "question": "Q one?", "notes": "n"},
            {"category": "b", "question": "Q two?"},
            {"category": "c", "question": "Q one?"}]
    assert _seed(monkeypatch, fresh_session, tmp_path, rows) == {"seeded": 2}
    assert lines == []
    assert _stored(fresh_session) == {"Q one?": ("a", "n"), "Q two?": ("b", "")}


def test_control_the_shipped_seed_file_seeds_every_row(monkeypatch, fresh_session, tmp_path):
    if not SEED_FILE.exists():
        pytest.skip("the seed file is not beside this suite")
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    answer = _seed(monkeypatch, fresh_session, tmp_path, rows)
    assert answer == {"seeded": len({r["question"].strip() for r in rows})}
