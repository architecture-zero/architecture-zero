"""No log line carries an exception's words unbounded (2026-10-31).

The rule for an internal error's detail in a log is 300 characters, the type
first: f"{type(e).__name__}: {e}"[:300]. An exception's words are not this
code's own sentence - a driver's message can carry a query, a path, a URL with
its parameters, or a whole response body - and a log line is read, shipped and
kept. The lines filed one at a time were bounded; more were found each time
someone looked (the chat routes, the startup hooks, the alert mailer, the eval
runner, the maintenance passes). This scans the backend's source so the class
stays closed rather than being found again by hand.

WHAT IT FLAGS. Inside an `except ... as NAME:` block, a log call - log(),
log_error(), any logger method, warnings.warn() or print() - one of whose
arguments renders NAME's text (NAME itself, str/repr of it, an f-string or a
%-format of it, or a text attribute of it) without a slice of at most 300
around it. type(NAME), isinstance() and the numeric attributes (status_code,
returncode, errno) are not its words.

WHAT IT LEAVES. A logged traceback - logger.exception() or exc_info= - is a
separate thing and stays. So does a value computed into a variable first and
logged later: the scan reads one expression, and says so here rather than
claiming more.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

_LEVELS = {"debug", "info", "warning", "warn", "error", "critical", "log"}
_PLAIN = {"log", "log_error", "print"}
# Calls whose answer is not the exception's words.
_SAFE_CALLS = {"type", "isinstance", "len", "id", "bool", "hasattr"}
# Attributes that are numbers, not words.
_SAFE_ATTRS = {"status_code", "returncode", "errno", "code", "status", "lineno"}
_CAP = 300


def _is_log_call(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id in _PLAIN
    if isinstance(f, ast.Attribute):
        if f.attr == "exception":
            return False                     # a traceback: left as it is
        return f.attr in _LEVELS
    return False


def _carries_traceback(call: ast.Call) -> bool:
    return any(k.arg == "exc_info" for k in call.keywords)


def _bounded_slice(node: ast.Subscript) -> bool:
    s = node.slice
    return (isinstance(s, ast.Slice) and s.step is None
            and (s.lower is None or (isinstance(s.lower, ast.Constant) and s.lower.value == 0))
            and isinstance(s.upper, ast.Constant) and isinstance(s.upper.value, int)
            and 0 < s.upper.value <= _CAP)


def _unbounded(node: ast.AST, name: str) -> bool:
    """True when `node` renders the exception's text with no bound around it."""
    if isinstance(node, ast.Subscript) and _bounded_slice(node):
        return False
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _SAFE_CALLS:
        return False
    if isinstance(node, ast.Attribute) and node.attr in _SAFE_ATTRS:
        return False
    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(_unbounded(child, name) for child in ast.iter_child_nodes(node))


def _findings(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(APP.parent).as_posix()
    out = set()
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name):
            continue
        for stmt in handler.body:
            for call in ast.walk(stmt):
                if not (isinstance(call, ast.Call) and _is_log_call(call)):
                    continue
                if _carries_traceback(call):
                    continue
                args = list(call.args) + [k.value for k in call.keywords]
                if any(_unbounded(a, handler.name) for a in args):
                    out.add(f"{rel}:{call.lineno}")
    return sorted(out)


def _modules():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_log_call_carries_an_exception_text_unbounded():
    found = [f for p in _modules() for f in _findings(p)]
    assert found == [], (
        "a log call renders an exception's text with no bound - log "
        "f\"{type(e).__name__}: {e}\"[:300] instead:\n  " + "\n  ".join(found))


# -- The scanner itself: what it flags and what it leaves ---------------------

def _scan(src: str) -> int:
    tree = ast.parse(src)
    n = 0
    for handler in ast.walk(tree):
        if isinstance(handler, ast.ExceptHandler) and handler.name:
            for stmt in handler.body:
                for call in ast.walk(stmt):
                    if isinstance(call, ast.Call) and _is_log_call(call) \
                            and not _carries_traceback(call):
                        args = list(call.args) + [k.value for k in call.keywords]
                        n += any(_unbounded(a, handler.name) for a in args)
    return n


def test_the_scan_flags_each_unbounded_shape():
    for line in ('log_error("x", error=str(e))',
                 'log("x", error=f"failed: {e}")',
                 'logger.warning("failed: %s", e)',
                 'log.error("failed: %r", e)',
                 'logging.getLogger("x").warning(f"failed: {e!r}")',
                 'print("failed: %s" % e, flush=True)',
                 'log_error("x", error=str(e)[:5000])',
                 'log_error("x", error=e.stderr)'):
        src = f"try:\n    pass\nexcept Exception as e:\n    {line}\n"
        assert _scan(src) == 1, line


def test_the_scan_leaves_the_bounded_and_the_tracebacks():
    for line in ('log_error("x", error=f"{type(e).__name__}: {e}"[:300])',
                 'log_error("x", error=str(e)[:200])',
                 'log_error("x", kind=type(e).__name__)',
                 'log_error("x", status=e.status_code)',
                 'logger.exception("failed: %s", e)',
                 'logger.error("failed: %s", e, exc_info=True)',
                 'log_error("x", error="a fixed sentence")'):
        src = f"try:\n    pass\nexcept Exception as e:\n    {line}\n"
        assert _scan(src) == 0, line
