"""The seed sync names every row it cannot sync, and stores only well-formed
setup turns.

Before this change a seed row that was not an object (a bare string, a
number, a list) raised AttributeError at q.get and stopped the whole sync -
the startup sync logged a warning and the admin sync route answered 500. A
row with an empty question was skipped in silence. A category or notes that
was not text raised at strip() the same way, and an expected_source or
as_level of the wrong type was stored and stopped a run later. A setup_turns
list with items that are not turns was stored whole and those items were
dropped, silently, at read.

Now each such row is handled as a repeated question already was: the row is
skipped, the rest of the file syncs, and it is named - in the result's
"refused" (its place in the file, its id when the question is already
stored, its text, the rule) and so in the eval_seed_sync log line - with an
error line of its own (eval_seed_row_refused). Nothing of it is written. A
setup_turns list keeps only its well-formed turns (the read's own rule,
_well_formed_turns) and a row that loses any is named in "turns_dropped"
with how many, with its own error line (eval_seed_turns_dropped); a list
with no well-formed turn at all is refused. A list of good turns is stored
byte for byte as before.

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
ROW_RULE = "a seed row must be a JSON object"
EMPTY_RULE_START = "empty question"


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
def seeded(monkeypatch, fresh_session, tmp_path):
    """sync(rows) -> (result, error lines); the store is fresh_session."""
    import app.eval_runner as runner
    errors: list = []
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "log_error", lambda event, **f: errors.append((event, f)))

    def sync(rows):
        path = tmp_path / "eval-seed.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        monkeypatch.setattr(runner, "EVAL_SEED_PATH", str(path))
        errors.clear()
        return runner.sync_eval_questions_from_seed(), list(errors)
    return sync


def _stored(fresh_session):
    from app.models import EvalQuestion
    with fresh_session() as db:
        return {r.question: {"id": r.id, "category": r.category,
                             "expected_source": r.expected_source,
                             "setup_turns": r.setup_turns}
                for r in db.query(EvalQuestion).all()}


# -- 1. a row that is not an object ------------------------------------------------

@pytest.mark.parametrize("bad", ["a bare string", 7, ["a", "list"], None, True], ids=repr)
def test_fix_a_row_that_is_not_an_object_is_named_and_the_rest_sync(
        seeded, fresh_session, bad):
    result, errors = seeded([{"question": "Q one?"}, bad, {"question": "Q two?"}])
    assert result["added"] == 2
    assert result["refused"] == [{"index": 1, "id": None, "question": None, "rule": ROW_RULE}]
    assert errors == [("eval_seed_row_refused",
                       {"index": 1, "question_id": None, "rule": ROW_RULE})]
    assert sorted(_stored(fresh_session)) == ["Q one?", "Q two?"]


def test_fix_the_admin_sync_route_answers_its_result(client, admin_headers, monkeypatch,
                                                    fresh_session, tmp_path):
    import app.eval_runner as runner
    path = tmp_path / "eval-seed.json"
    path.write_text(json.dumps([{"question": "Q one?"}, "stray", {"question": "Q two?"}]),
                    encoding="utf-8")
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", str(path))
    r = client.post("/api/admin/evals/questions/sync", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2
    assert [x["index"] for x in body["refused"]] == [1]


# -- 2. an empty question ------------------------------------------------------------

@pytest.mark.parametrize("row", [{"question": ""}, {"question": "   "}, {},
                                 {"question": None}, {"question": 5}], ids=repr)
def test_fix_an_empty_question_is_named(seeded, fresh_session, row):
    result, errors = seeded([{"question": "Q one?"}, row])
    assert result["added"] == 1
    (refused,) = result["refused"]
    assert (refused["index"], refused["id"], refused["question"]) == (1, None, None)
    assert refused["rule"].startswith(EMPTY_RULE_START)
    assert errors[0][0] == "eval_seed_row_refused"
    assert sorted(_stored(fresh_session)) == ["Q one?"]


# -- a field the sync or a run reads as text, or as a level ----------------------------

@pytest.mark.parametrize("field, value, rule", [
    ("category", 5, "category must be text"),
    ("notes", ["x"], "notes must be text"),
    ("expected_source", 7, "expected_source must be text"),
    ("as_level", "2", "as_level must be an integer"),
    ("as_level", True, "as_level must be an integer"),
], ids=lambda v: repr(v)[:20])
def test_fix_a_field_of_the_wrong_type_is_refused_with_the_id(seeded, fresh_session,
                                                             field, value, rule):
    first, _ = seeded([{"question": "Q kept?", "category": "before"}])
    assert first["added"] == 1
    before = _stored(fresh_session)["Q kept?"]
    result, errors = seeded([{"question": "Q kept?", "category": "after", field: value},
                             {"question": "Q new?", field: value}])
    assert [(x["index"], x["id"], x["rule"]) for x in result["refused"]] == [
        (0, before["id"], rule), (1, None, rule)]
    assert [e[1]["question_id"] for e in errors] == [before["id"], None]
    assert _stored(fresh_session) == {"Q kept?": before}


# -- 3. a list with items that are not turns -------------------------------------------

def test_fix_only_the_well_formed_turns_are_stored_and_the_row_is_named(seeded, fresh_session):
    mixed = [GOOD[0], 7, {"role": "user"}, {"role": "system", "content": "x"}, GOOD[1]]
    result, errors = seeded([{"question": "Q mixed?", "setup_turns": mixed}])
    assert result["added"] == 1 and result.get("refused") == []
    (dropped,) = result["turns_dropped"]
    assert (dropped["index"], dropped["id"], dropped["question"], dropped["dropped"]) == \
        (0, None, "Q mixed?", 3)
    assert errors == [("eval_seed_turns_dropped",
                       {"index": 0, "question_id": None, "dropped": 3,
                        "rule": dropped["rule"]})]
    assert _stored(fresh_session)["Q mixed?"]["setup_turns"] == json.dumps(GOOD)


def test_fix_a_list_with_no_well_formed_turn_is_refused(seeded, fresh_session):
    result, errors = seeded([{"question": "Q none good?", "setup_turns": [7, "hi"]}])
    (refused,) = result["refused"]
    assert (refused["index"], refused["question"]) == (0, "Q none good?")
    assert refused["rule"].startswith("setup_turns holds no well-formed turn")
    assert "Q none good?" not in _stored(fresh_session)


def test_control_a_good_list_and_an_absent_value_store_as_before(seeded, fresh_session):
    result, errors = seeded([{"question": "Q list?", "setup_turns": GOOD},
                             {"question": "Q none?"},
                             {"question": "Q empty?", "setup_turns": []}])
    assert result["added"] == 3
    assert result.get("refused", []) == [] and result.get("turns_dropped", []) == []
    assert errors == []
    stored = _stored(fresh_session)
    assert stored["Q list?"]["setup_turns"] == json.dumps(GOOD)
    assert stored["Q none?"]["setup_turns"] is None
    assert stored["Q empty?"]["setup_turns"] is None


def test_control_the_shipped_seed_syncs_with_nothing_refused(seeded):
    if not SEED_FILE.exists():
        pytest.skip("the seed file is not beside this suite")
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    result, errors = seeded(rows)
    assert result["added"] == len(rows)
    assert result.get("refused", []) == [] and result.get("turns_dropped", []) == []
    assert errors == []
