"""No route answers a caller with an exception's words (2026-10-31).

A route's refusal is read by whoever called it: the HTTPException's detail is
a sentence this code wrote, and the exception's own words - a library's, the
resolver's, the store's - go to the log, bounded (type first, 300). The KB
upload answered a file it could not read with "Failed to extract text: " and
the parser's words, and a peer URL whose host did not resolve was refused
with the resolver's words inside the refusal the peer routes answer.

THE FIRST SCAN reads app/ for an `except ... as NAME:` block that raises an
HTTPException whose detail renders NAME's words at all - bounded or not: a
bound protects a log line, but a caller is owed a fixed sentence. It leaves a
handler that catches only the code's OWN refusals (_OWN_SENTENCE below): each
is raised with a sentence this code wrote, which the detail may carry on.

THE SECOND SCAN holds that rule: inside an `except ... as NAME:` block, an
own refusal is never raised with NAME's words. That is how the resolver's
words reached the peer routes.

WHAT IT LEAVES. A value computed into a variable first and rendered later:
the scan reads one expression. The behaviour tests below cover the two sites
this change closed.
"""
import ast
import logging

import pytest
from fastapi import HTTPException

from tests.test_logged_exception_text_bounded import APP, _modules

# Refusals whose every raise is a sentence this code wrote: the type, and the
# module that defines it (so a rename fails here instead of widening the scan).
_OWN_SENTENCE = {
    "ExtractError": "app/text_extract.py",
    "JobQueueFull": "app/jobs.py",
    "PeerURLRefused": "app/peers.py",
}

# Calls and attributes whose answer is not the exception's words.
_SAFE_CALLS = {"type", "isinstance", "len", "id", "bool", "hasattr"}
_SAFE_ATTRS = {"status_code", "returncode", "errno", "code", "status", "lineno",
               "unsupported"}


def _renders(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _SAFE_CALLS:
        return False
    if isinstance(node, ast.Attribute) and node.attr in _SAFE_ATTRS:
        return False
    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(_renders(child, name) for child in ast.iter_child_nodes(node))


def _type_names(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        return [n for elt in node.elts for n in _type_names(elt)]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    return ["?"]


def _called(call: ast.Call) -> str | None:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _detail(call: ast.Call) -> ast.AST | None:
    for k in call.keywords:
        if k.arg == "detail":
            return k.value
    return call.args[1] if len(call.args) > 1 else None


def _scan(src: str, rel: str) -> tuple[list[str], list[str]]:
    """(route details rendering an exception, own refusals carrying one)."""
    details, own = set(), set()
    for handler in ast.walk(ast.parse(src)):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name):
            continue
        only_own = all(t in _OWN_SENTENCE for t in _type_names(handler.type))
        for stmt in handler.body:
            for call in ast.walk(stmt):
                if not isinstance(call, ast.Call):
                    continue
                name = _called(call)
                if name == "HTTPException" and not only_own:
                    d = _detail(call)
                    if d is not None and _renders(d, handler.name):
                        details.add(f"{rel}:{call.lineno}")
                elif name in _OWN_SENTENCE:
                    args = list(call.args) + [k.value for k in call.keywords]
                    if any(_renders(a, handler.name) for a in args):
                        own.add(f"{rel}:{call.lineno}")
    return sorted(details), sorted(own)


def _all():
    details, own = [], []
    for p in _modules():
        d, o = _scan(p.read_text(encoding="utf-8"), p.relative_to(APP.parent).as_posix())
        details += d
        own += o
    return details, own


def test_no_route_detail_renders_an_exception():
    details, _ = _all()
    assert details == [], (
        "a route answers with an exception's words - answer a fixed sentence "
        "and log f\"{type(e).__name__}: {e}\"[:300]:\n  " + "\n  ".join(details))


def test_no_own_refusal_carries_another_exceptions_words():
    _, own = _all()
    assert own == [], (
        "an own refusal is raised with an exception's words, and its sentence "
        "reaches a caller - log them, bounded, instead:\n  " + "\n  ".join(own))


def test_the_own_refusals_are_defined_where_named():
    for cls, rel in _OWN_SENTENCE.items():
        tree = ast.parse((APP.parent / rel).read_text(encoding="utf-8"))
        assert any(isinstance(n, ast.ClassDef) and n.name == cls for n in ast.walk(tree)), (cls, rel)


def test_the_scan_flags_each_shape_and_leaves_the_fixed():
    def scan(caught, line):
        src = f"def f():\n    try:\n        pass\n    except {caught} as e:\n        {line}\n"
        return _scan(src, "probe")

    for line in ('raise HTTPException(status_code=422, detail=f"Failed: {e}")',
                 'raise HTTPException(400, str(e))',
                 'raise HTTPException(status_code=400, detail=str(e)[:300])',
                 'raise HTTPException(status_code=400, detail=e.args[0])'):
        assert scan("Exception", line)[0], line
    assert scan("ValueError", 'raise PeerURLRefused(f"no: {e}")')[1]
    for line in ('raise HTTPException(status_code=422, detail="A fixed sentence.")',
                 'raise HTTPException(status_code=e.status_code, detail="Fixed.")',
                 'raise HTTPException(400 if e.unsupported else 422, detail="Fixed.")'):
        assert scan("Exception", line) == ([], []), line
    # The code's own refusal carries its own sentence on.
    assert scan("PeerURLRefused", 'raise HTTPException(400, detail=str(e))') == ([], [])
    assert scan("(jobs.JobQueueFull, ExtractError)", 'raise HTTPException(503, detail=str(e))') == ([], [])


# -- The two sites this closed ---------------------------------------------------

_LONG = "x" * 2000


def test_a_file_that_cannot_be_read_answers_a_fixed_sentence(monkeypatch, caplog):
    from app.routers import kb
    seen = []
    monkeypatch.setattr(kb, "log_error", lambda event, **kw: seen.append((event, kw)))

    def _boom(name, data):
        raise RuntimeError("the parser's words " + _LONG)

    monkeypatch.setattr("app.text_extract.extract_text", _boom)
    with pytest.raises(HTTPException) as raised:
        kb._ingest_upload("a.pdf", "pdf", b"%PDF-", "general", {"id": 1, "role": "owner"})
    assert raised.value.status_code == 422
    assert raised.value.detail == kb.EXTRACT_FAILED_DETAIL
    assert "parser" not in raised.value.detail
    [(_, fields)] = [e for e in seen if e[0] == "ingest_extract_failed"]
    assert fields["error"].startswith("RuntimeError: the parser's words")
    assert len(fields["error"]) <= 300


def test_a_host_that_does_not_resolve_is_refused_in_the_codes_own_words(monkeypatch, caplog):
    import socket

    from app import peers

    def _gai(*a, **k):
        raise socket.gaierror(11001, "the resolver's words " + _LONG)

    monkeypatch.setattr(peers.socket, "getaddrinfo", _gai)
    with caplog.at_level(logging.WARNING, logger="app.peers"):
        with pytest.raises(peers.PeerURLRefused) as raised:
            peers.validate_peer_url("https://nowhere.invalid")
    assert str(raised.value) == "Peer host 'nowhere.invalid' does not resolve"
    [line] = [r.getMessage() for r in caplog.records if "does not resolve" in r.getMessage()]
    assert "gaierror: " in line and "resolver's words" in line
    assert len(line) <= 400
