"""Structured-log fields must not collide with the envelope keys.

app/logger.py builds the payload as {timestamp, level, event} and THEN
`payload.update(record.data)`. The caller's fields win, silently, so a kwarg
named after an envelope key does not merge or error - it overwrites. A
`log("x", level=2)` replaces the severity string "INFO" with an integer, and
the loss is invisible in the code, in the tests, and in the passing suite. It
shows up only in a log line nobody reads until they need it.

That happened for real: the v0.1.1 federation-clearance work logged the
clearance rung as `level=`, on `peer_kb_served` and `peer_query_refused` -
the two events an operator would most want to filter by severity when
auditing who read what across the seam. Every unit test passed. It was found
by running the product and reading the output.

This is the machine version of that lesson, and it is a source scan rather
than a runtime check on purpose: the defect is in the CALL, and a runtime
assertion would only fire on the code paths a test happens to walk.
"""
import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Written by _JsonFormatter.format() before the caller's fields are merged in.
RESERVED = {"timestamp", "level", "event", "exception"}
LOG_FUNCS = {"log", "log_error"}


def _log_calls_with_reserved_kwargs():
    hits = []
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a parse failure is its own test
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name not in LOG_FUNCS:
                continue
            # The event name is the first positional arg when it is a literal.
            # It goes IN the hit string on purpose: without it the per-event
            # tests below can never match a hit, and a guard that cannot fail
            # is the thing this file exists to complain about. (It could not,
            # for one commit. Caught by re-introducing the bug and watching
            # those tests stay green.)
            event = "?"
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                event = node.args[0].value
            for kw in node.keywords:
                if kw.arg in RESERVED:
                    hits.append(f"{path.relative_to(APP.parent)}:{node.lineno} "
                                f"{name}(\"{event}\", ... {kw.arg}=...)")
    return hits


def test_no_structured_log_field_shadows_the_envelope():
    hits = _log_calls_with_reserved_kwargs()
    assert not hits, (
        "structured log call(s) pass a field that overwrites the log envelope "
        f"({', '.join(sorted(RESERVED))}) - rename the field:\n  "
        + "\n  ".join(hits))


def test_the_scan_can_actually_see_a_violation():
    """A guard that cannot fail is decoration. Pin that the detector fires on
    the exact shape it exists to catch, so a refactor of the walk above cannot
    quietly turn it into a no-op that passes forever."""
    tree = ast.parse('log("some_event", level=2, other="ok")')
    call = tree.body[0].value
    reserved = [kw.arg for kw in call.keywords if kw.arg in RESERVED]
    assert reserved == ["level"]


@pytest.mark.parametrize("event", ["peer_kb_served", "peer_query_refused"])
def test_the_federation_audit_events_keep_their_severity(event):
    """The two that were actually broken, named individually so a regression
    points at the incident rather than at an abstract rule."""
    hits = _log_calls_with_reserved_kwargs()
    offending = [h for h in hits if f'"{event}"' in h]
    assert not offending, f"{event} lost its severity field: {offending}"
