"""Two structural rules the router split depends on, enforced by machine.

Both were stated in docstrings and commit messages first, and both had already
slipped once by the end of the first router extraction - which is the argument
for checking them here instead of remembering them nine more times.

There is no linter in CI (the four jobs are residue, gitleaks, tests, image),
so these run in the suite.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _modules():
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_nothing_under_app_imports_app_main():
    """Direction is main -> routers -> runtime_config, one way.

    A router reaching back into main is not a clean ImportError: main imports
    the routers partway through its own module body, so a back-reference sees a
    half-built module where every name defined below that point is simply
    absent. Imported lazily inside a handler it resolves fine at request time
    and the cycle ships undetected - so the check is structural, not runtime.
    """
    offenders = []
    for path in _modules():
        if path.name == "main.py":
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and (node.module or "") in ("app.main", "main"):
                offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("app.main", "main"):
                        offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    assert not offenders, (
        "modules under app/ importing app.main - move the shared name into "
        f"app/runtime_config.py instead: {offenders}")


# Names that are imported for their side effect or re-exported on purpose.
# Keep this list short and state the reason; it is the escape hatch that would
# otherwise let the whole check rot.
_ALLOWED_UNUSED = {
    # app/providers.py declares the key names as module API even where this
    # module does not read them itself.
    ("main.py", "OPENAI_KEY"),
}


def test_no_dead_module_level_imports_under_app():
    """A leftover unused import in main.py is a live patch("app.main.X") target
    that reaches no caller: the patch succeeds, injects nothing, and the test
    passes while testing nothing. That is the same shape as the trap the split
    deliberately avoided by not re-exporting BACKUP_STATUS_DIR - and _ollama_get
    moving to runtime_config left exactly one behind on the first try.
    """
    dead = []
    for path in _modules():
        tree = _tree(path)
        bound = {}
        for node in tree.body:                     # module level only
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    name = (alias.asname or alias.name).split(".")[0]
                    bound[name] = node.lineno
        if not bound:
            continue
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for node in ast.walk(tree):                # names reached via strings
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                used.add(node.value)
        for name, lineno in bound.items():
            if name in used:
                continue
            if (path.name, name) in _ALLOWED_UNUSED:
                continue
            dead.append(f"{path.relative_to(APP.parent)}:{lineno} {name}")
    assert not dead, (
        "module-level imports with no reader. Prune them in the same commit "
        "that orphaned them, or add to _ALLOWED_UNUSED with a reason:\n  "
        + "\n  ".join(sorted(dead)))
