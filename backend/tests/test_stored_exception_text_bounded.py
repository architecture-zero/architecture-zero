"""Exception text kept OUTSIDE a log line is bounded too (2026-10-31).

test_logged_exception_text_bounded.py holds every log call to the rule - an
exception's words at most 300 characters, the type first. The same words were
also kept where no log line goes: stored into a result an Owner's or an
Admin's screen reads, written onto a row, or recorded as a peer's last error.
The detailed health view's db_error, the reranker's load error and its
self-test error, the index maintenance summary, the eval runner's errored
answer row and its crashed-run status, the judge's error, the knowledge sync's
and the system records' per-source results (POST /api/kb/sync answers them,
and that route is manage_kb), a failed release's stored error, and a peer's
last_error. Each now keeps f"{type(e).__name__}: {e}"[:300] (inside the
sentence it already had: "[ERROR: ...]", "rerank call failed: ...").

THE SCAN reads app/ for an `except ... as NAME:` block that stores NAME's
words, unbounded, into a subscript or an attribute, or as a value of a dict
literal. Storing the exception object itself is not its words. It does not
follow a value computed into a plain variable first - the behaviour tests
below cover the ones that do (the eval row, the judge, the peer record).

self_check.probe_disk's {"error": str(e)} was filed here, not fixed, on
2026-10-31: the module is one text on every surface, so its change travels
as that file, never alone. It is bounded now, in the same commit as the
module, and nothing is left filed.
"""
import ast
import pathlib

from tests.test_logged_exception_text_bounded import APP, _bounded_slice, _modules, _unbounded

_LONG = "x" * 2000


def _words(node: ast.AST, name: str) -> bool:
    """The exception's words, unbounded - not the exception object itself."""
    if isinstance(node, ast.Name) and node.id == name:
        return False
    return _unbounded(node, name)


def _stored(path: pathlib.Path) -> list[tuple[str, str, int]]:
    return _stored_in(path.read_text(encoding="utf-8"), path.relative_to(APP.parent).as_posix())


def _stored_in(src: str, rel: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(src)
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                owner.setdefault(id(n), fn.name)
    out = set()
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name):
            continue
        for stmt in handler.body:
            for n in ast.walk(stmt):
                hit = False
                if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and n.value is not None:
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    hit = (any(isinstance(t, (ast.Subscript, ast.Attribute)) for t in targets)
                           and _words(n.value, handler.name))
                elif isinstance(n, ast.Dict):
                    hit = any(v is not None and _words(v, handler.name) for v in n.values)
                if hit:
                    out.add((rel, owner.get(id(n), "<module>"), n.lineno))
    return sorted(out)


def test_no_exception_text_is_stored_unbounded():
    found = [f for p in _modules() for f in _stored(p)]
    assert found == [], (
        "exception text stored with no bound - keep "
        "f\"{type(e).__name__}: {e}\"[:300] instead:\n  "
        + "\n  ".join(f"{r}:{line} ({fn})" for r, fn, line in found))


def test_the_scan_flags_a_store_and_leaves_the_bounded_and_the_object():
    def scan(line):
        return _stored_in(
            f"def f():\n    try:\n        pass\n    except Exception as e:\n        {line}\n", "probe")
    for line in ('out["error"] = str(e)', 'row.error = f"failed: {e}"',
                 'out[k] = {"status": "error", "error": repr(e)}', 'out["e"] = str(e)[:500]'):
        assert scan(line), line
    for line in ('out["error"] = f"{type(e).__name__}: {e}"[:300]', 'got["e"] = e',
                 'out["kind"] = type(e).__name__', 'out[k] = {"error": "a fixed sentence"}'):
        assert not scan(line), line
    assert _bounded_slice(ast.parse('x[:300]').body[0].value)


# -- What the scan cannot follow: a value computed first --------------------------

def test_the_eval_row_keeps_the_error_bounded_and_marked(monkeypatch):
    """The errored answer row is stored and judged as a FAIL by its prefix."""
    import app.eval_runner as runner
    from app.db import get_session
    from app.models import EvalResult

    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(runner, "supports_tools", lambda model="": False)
    for judge in ("judge_answer", "judge_faithfulness", "judge_freshness"):
        monkeypatch.setattr(f"app.eval_judge.{judge}", lambda *a, **k: (0, "ok"))

    def _writer(msgs, model, system_prompt="", max_tokens=0):
        raise RuntimeError(_LONG)
        yield ""

    monkeypatch.setattr(runner, "stream_chat", _writer)
    run_id = "stored-exc-eval"
    q = {"id": 9821, "question": "bounded error row 9821?", "category": "general",
         "expected_source": None, "notes": "", "as_level": None}
    try:
        runner._run_eval_job(run_id, "2026-01-01T00:00:00", [q], model="m",
                             use_rag=False, n_results=None, retrieval_only=False)
        with get_session() as s:
            [row] = s.query(EvalResult).filter(EvalResult.run_id == run_id).all()
            response = row.response
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as s:
            s.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    assert response.startswith("[ERROR: RuntimeError: xxx"), response[:80]
    assert response.endswith("]") and len(response) <= len("[ERROR: ]") + 300


def test_the_judge_error_is_bounded(monkeypatch):
    import app.eval_judge as judge

    def _boom(*a, **k):
        raise RuntimeError(_LONG)
        yield ""

    monkeypatch.setattr(judge, "stream_chat", _boom)
    score, reason = judge._run_judge("system", "user", "m")
    assert score is None
    assert reason.startswith("[judge error: RuntimeError: xxx"), reason[:80]
    assert len(reason) <= len("[judge error: ]") + 300


def test_a_peers_last_error_is_bounded(monkeypatch):
    from app import peers
    store = {}
    monkeypatch.setattr(peers, "get_peer_health", lambda pid: dict(store.get(pid, {})))
    monkeypatch.setattr(peers, "_save_peer_health", lambda pid, h: store.__setitem__(pid, h))
    peers._record_failure("bounded-peer", "a refusal naming " + _LONG)
    assert len(store["bounded-peer"]["last_error"]) == 300


def test_a_refused_peer_url_is_recorded_type_first(monkeypatch):
    from app import peers
    recorded = []
    monkeypatch.setattr(peers, "_circuit_open", lambda pid: False)
    monkeypatch.setattr(peers, "_record_failure", lambda pid, err: recorded.append(err))

    def _refuse(url, require_resolution=False):
        raise peers.PeerURLRefused("Peer host 'h' does not resolve: " + _LONG)

    monkeypatch.setattr(peers, "resolve_peer_url", _refuse)
    assert peers.query_peer_kb({"id": "p1", "name": "p", "url": "http://h"}, "q") == []
    assert recorded and recorded[0].startswith("PeerURLRefused: Peer host"), recorded
    assert len(recorded[0]) <= 300


def test_the_disk_probe_error_is_bounded_and_typed(monkeypatch):
    """probe_disk kept {"error": str(e)}, whole and untyped; the detailed
    health view serves it (2026-10-31)."""
    from app import self_check

    def _boom(path):
        raise OSError(_LONG)

    monkeypatch.setattr(self_check.shutil, "disk_usage", _boom)
    err = self_check.probe_disk("/nowhere")["error"]
    assert err.startswith("OSError: xxx") and len(err) <= 300, err[:80]


def test_the_detailed_health_db_error_is_bounded(client, admin_headers, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError(_LONG)

    monkeypatch.setattr("sqlalchemy.text", _boom)
    r = client.get("/api/health/detailed", headers=admin_headers)
    assert r.status_code == 200, r.text
    err = r.json()["db_error"]
    assert err.startswith("RuntimeError: xxx") and len(err) <= 300, err[:80]
