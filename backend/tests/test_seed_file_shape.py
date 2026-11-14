"""A seed FILE that is not JSON, or whose top level is not a list, is refused
whole.

Before this change json.load raised on a file that is not JSON, and a top
level that is not a list either raised in the loop (a number, null) or was
walked as rows (an object's keys, a string's characters). The boot's sync
caught the error and logged it as a warning; the admin sync route and the
seed route answered 500.

Now eval_runner._read_seed_file is the one read of the file: a file that is
not JSON (a byte that is not UTF-8 included) or whose top level is not a
list is refused with its rule. The sync answers {"status": "refused",
"rule": ...} and writes nothing, the boot carries on with one error line
(eval_seed_file_refused) naming the rule, and both admin routes answer 400
with the rule. Nothing of the file is echoed anywhere.

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
MARKER = "Zq7-file-marker"
NOT_JSON = "the seed file is not valid JSON: it must hold a JSON list of questions"
NOT_A_LIST = "the seed file must hold a JSON list of questions"
FILES = {
    "not json": ('{"question": "' + MARKER + '"', NOT_JSON),
    "empty": ("", NOT_JSON),
    "not utf-8": (b'[{"question": "\xff\xfe' + MARKER.encode() + b'"}]', NOT_JSON),
    "an object": ('{"question": "' + MARKER + '"}', NOT_A_LIST),
    "a number": ("7", NOT_A_LIST),
    "null": ("null", NOT_A_LIST),
    "a string": ('"' + MARKER + '"', NOT_A_LIST),
}


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


def _write(tmp_path, content):
    path = tmp_path / "eval-seed.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def sync(monkeypatch, fresh_session, tmp_path):
    """sync(content) -> (result, error lines, rows stored)."""
    import app.eval_runner as runner
    errors: list = []
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "log_error", lambda event, **f: errors.append((event, f)))

    def run(content):
        monkeypatch.setattr(runner, "EVAL_SEED_PATH", _write(tmp_path, content))
        errors.clear()
        result = runner.sync_eval_questions_from_seed()
        from app.models import EvalQuestion
        with fresh_session() as db:
            stored = db.query(EvalQuestion).count()
        return result, list(errors), stored
    return run


@pytest.mark.parametrize("name", list(FILES))
def test_fix_the_sync_refuses_the_whole_file_in_words(sync, name):
    content, rule = FILES[name]
    result, errors, stored = sync(content)
    assert result == {"status": "refused", "rule": rule}
    assert errors == [("eval_seed_file_refused", {"rule": rule})]
    assert stored == 0
    assert MARKER not in json.dumps(result) + json.dumps(errors)


@pytest.mark.parametrize("name", ["not json", "an object", "a number"])
def test_fix_the_admin_sync_route_answers_400_with_the_rule(client, admin_headers, monkeypatch,
                                                           fresh_session, tmp_path, name):
    import app.eval_runner as runner
    content, rule = FILES[name]
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH", _write(tmp_path, content))
    r = client.post("/api/admin/evals/questions/sync", headers=admin_headers)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == rule
    assert MARKER not in r.text


@pytest.mark.parametrize("name", ["not json", "an object", "a number"])
def test_fix_the_seed_route_answers_400_with_the_rule(client, admin_headers, monkeypatch,
                                                     tmp_path, name):
    import app.routers.evals as evals
    content, rule = FILES[name]
    monkeypatch.setattr(evals, "EVAL_SEED_PATH", _write(tmp_path, content))
    r = client.post("/api/admin/evals/questions/seed", headers=admin_headers)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == rule
    assert MARKER not in r.text


def test_control_the_shipped_seed_file_syncs_as_before(sync):
    if not SEED_FILE.exists():
        pytest.skip("the seed file is not beside this suite")
    rows = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    result, errors, stored = sync(SEED_FILE.read_bytes())
    assert result["status"] == "ok"
    assert result["added"] == stored == len(rows)
    assert errors == []


def test_control_the_admin_sync_route_answers_its_result(client, admin_headers, monkeypatch,
                                                        fresh_session, tmp_path):
    import app.eval_runner as runner
    monkeypatch.setattr(runner, "get_session", fresh_session)
    monkeypatch.setattr(runner, "EVAL_SEED_PATH",
                        _write(tmp_path, json.dumps([{"question": "Q one?"}])))
    r = client.post("/api/admin/evals/questions/sync", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1
