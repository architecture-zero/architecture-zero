"""The seed route stores a row's setup turns as the sync does.

The seed route stored every field of a row but its setup_turns, so a
multi-turn question it seeded was asked as a single turn until the next
sync filled the turns in. Its kept rows are now stored through
eval_runner._seed_values, setup_turns included (_seed_turns: the
well-formed turns as a JSON string, the read's own rule): a multi-turn row
is multi-turn at once and the next sync finds nothing to change. A row that
loses turn items says so with the sync's eval_seed_turns_dropped line.

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
GOOD = [{"role": "user", "content": "How are departments kept apart?"},
        {"role": "assistant", "content": "Each has its own collection."}]


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


def _seed(monkeypatch, fresh_session, tmp_path, rows):
    import app.routers.evals as evals
    monkeypatch.setattr(evals, "get_session", fresh_session)
    monkeypatch.setattr(evals, "EVAL_SEED_PATH", _seed_file(tmp_path, rows))
    return evals.seed_eval_questions(current_user={})


def _sync(monkeypatch, fresh_session, tmp_path, rows):
    import app.eval_runner as runner
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", _seed_file(tmp_path, rows))
    return runner.sync_eval_questions_from_seed()


def _turns(fresh_session):
    from app.models import EvalQuestion
    with fresh_session() as db:
        return {r.question: r.setup_turns for r in db.query(EvalQuestion).all()}


def test_fix_a_seeded_multi_turn_row_is_multi_turn_at_once(monkeypatch, fresh_session, tmp_path):
    from app.eval_runner import _setup_turns
    rows = [{"category": "a", "question": "tell me more", "setup_turns": GOOD}]
    assert _seed(monkeypatch, fresh_session, tmp_path, rows) == {"seeded": 1}
    stored = _turns(fresh_session)["tell me more"]
    assert stored == json.dumps(GOOD)
    assert _setup_turns(stored) == GOOD
    result = _sync(monkeypatch, fresh_session, tmp_path, rows)
    assert (result["added"], result["updated"], result["unchanged"]) == (0, 0, 1)


def test_fix_a_seeded_row_keeps_only_its_well_formed_turns(monkeypatch, fresh_session, tmp_path,
                                                          lines):
    rows = [{"category": "a", "question": "tell me more",
             "setup_turns": [GOOD[0], 7, {"role": "user"}, GOOD[1]]}]
    assert _seed(monkeypatch, fresh_session, tmp_path, rows) == {"seeded": 1}
    assert _turns(fresh_session)["tell me more"] == json.dumps(GOOD)
    assert ("eval_seed_turns_dropped",
            {"index": 0, "question_id": None, "dropped": 2,
             "rule": lines[0][1]["rule"]}) == lines[0]
    result = _sync(monkeypatch, fresh_session, tmp_path, rows)
    assert (result["updated"], result["unchanged"]) == (0, 1)


def test_fix_the_shipped_seed_file_seeded_is_one_the_sync_leaves_alone(monkeypatch,
                                                                       fresh_session, tmp_path):
    if not SEED_FILE.exists():
        pytest.skip("the seed file is not beside this suite")
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    if not any(r.get("setup_turns") for r in rows):
        pytest.skip("the shipped seed file has no multi-turn row")
    assert _seed(monkeypatch, fresh_session, tmp_path, rows) == {"seeded": len(rows)}
    result = _sync(monkeypatch, fresh_session, tmp_path, rows)
    assert (result["added"], result["updated"]) == (0, 0)
    assert result["unchanged"] == len(rows)


def test_control_a_single_turn_row_seeds_as_before(monkeypatch, fresh_session, tmp_path, lines):
    rows = [{"category": "a", "question": "Q single?"},
            {"category": "b", "question": "Q empty turns?", "setup_turns": []}]
    assert _seed(monkeypatch, fresh_session, tmp_path, rows) == {"seeded": 2}
    assert _turns(fresh_session) == {"Q single?": None, "Q empty turns?": None}
    assert lines == []


def test_control_the_sync_stores_a_multi_turn_row_as_before(monkeypatch, fresh_session, tmp_path):
    rows = [{"category": "a", "question": "tell me more", "setup_turns": GOOD}]
    result = _sync(monkeypatch, fresh_session, tmp_path, rows)
    assert result["added"] == 1
    assert _turns(fresh_session)["tell me more"] == json.dumps(GOOD)
