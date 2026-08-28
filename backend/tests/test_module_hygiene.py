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


def _local_bindings(fn):
    """Names bound inside one function: its parameters plus its body.

    The parameters have to be taken from fn.args explicitly - fn.body does not
    contain them, so a body-only walk treats every parameter as resolving to
    module scope. A parameter that happens to share a name with a module-level
    import would then make that import look used.
    """
    a = fn.args
    bound = {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
    if a.vararg:
        bound.add(a.vararg.arg)
    if a.kwarg:
        bound.add(a.kwarg.arg)
    for node in fn.body:
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for alias in sub.names:
                    if alias.name != "*":
                        bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                bound.add(sub.id)
            elif isinstance(sub, ast.arg):
                bound.add(sub.arg)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                bound.add(sub.name)          # `except X as e` binds e here
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                # A NESTED def binds its own name in this scope, and it binds
                # via FunctionDef rather than via a Name Store. Missing this
                # made every inner helper look like it resolved to module scope.
                bound.add(sub.name)
    return bound


def _names_resolving_to_module_scope(tree):
    """Names read through the MODULE binding, not through a local rebind.

    Scope-aware on purpose, because the naive whole-tree walk counts a read
    inside a function that re-imports the same name locally - which is exactly
    how a dead module-level import hides. `optional_user` does
    `from app.users import get_user_by_id` and reads it, so the module-level
    import of that name looked used while reaching no caller: a live
    patch("app.main.get_user_by_id") target that injects nothing and keeps every
    test green.

    Deliberately NOT symtable, which was the first attempt: under PEP 709 a
    module-level list comprehension is inlined, and symtable still reports the
    names it reads as unreferenced at module scope - `re` in app/security.py is
    read only inside one, and got flagged as dead. Walking the AST with an
    explicit scope stack has no such gap.
    """
    seen, stack = set(), []

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.append(_local_bindings(node) if not isinstance(node, ast.Lambda)
                         else {a.arg for a in node.args.args})
            for child in ast.iter_child_nodes(node):
                visit(child)
            stack.pop()
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if not any(node.id in frame for frame in stack):
                seen.add(node.id)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return seen


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


# Names imported for a side effect or re-exported on purpose. Keep it short and
# state the reason - this is the escape hatch that would otherwise let the whole
# check rot. It is checked two-sided (see below): an entry that stops being
# needed fails, so the list cannot outlive its reasons.
_ALLOWED_UNUSED = set()


def test_no_dead_module_level_imports_under_app():
    """A leftover unused import in main.py is a live patch("app.main.X") target
    that reaches no caller: the patch succeeds, injects nothing, and the test
    passes while testing nothing. That is the same shape as the trap the split
    deliberately avoided by not re-exporting BACKUP_STATUS_DIR - and _ollama_get
    moving to runtime_config left exactly one behind on the first try.
    """
    dead, exemptions_used = [], set()
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
        used = _names_resolving_to_module_scope(tree)
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for node in ast.walk(tree):                # names reached via strings
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                used.add(node.value)
        for name, lineno in bound.items():
            if name in used:
                continue
            if (path.name, name) in _ALLOWED_UNUSED:
                exemptions_used.add((path.name, name))
                continue
            dead.append(f"{path.relative_to(APP.parent)}:{lineno} {name}")
    assert not dead, (
        "module-level imports with no reader. Prune them in the same commit "
        "that orphaned them, or add to _ALLOWED_UNUSED with a reason:\n  "
        + "\n  ".join(sorted(dead)))
    # Two-sided, same reasoning as PUBLIC_BY_DESIGN and REQUIRED_GUARD: an
    # exemption that stops being needed has to go, or the list becomes a place
    # where a real dead import can hide behind a stale reason.
    stale = sorted(_ALLOWED_UNUSED - exemptions_used)
    assert not stale, f"_ALLOWED_UNUSED entries no longer needed - remove them: {stale}"


def test_the_startup_ingest_flag_is_never_from_imported():
    """runtime_config._startup_ingest_active is REBOUND at runtime by main's
    startup hooks, and read by the evals router to refuse an eval mid-ingest.

    A `from app.runtime_config import _startup_ingest_active` anywhere binds
    False once at import time and never sees a rebind. The guard would then be
    permanently open: an eval started during a boot re-ingest returns 200 and
    measures a half-embedded corpus, with nothing in the logs to say so. The
    dead-import check cannot catch it either - the import is live and has a
    reader, it is just reading a fossil.

    So the rule is structural: the name may only be reached as an attribute.
    """
    offenders = []
    for path in _modules() + sorted((APP.parent / "tests").glob("test_*.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "_startup_ingest_active":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "_startup_ingest_active must be read as runtime_config._startup_ingest_active, "
        f"never from-imported - a from-import snapshots False forever: {offenders}")


def test_the_startup_ingest_flag_has_exactly_one_definition():
    """The other half: a second binding anywhere means main arms one copy while
    the router reads another. This is the assignment class the dead-import check
    is structurally blind to, so it is asserted by name here."""
    defs = []
    for path in _modules():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "_startup_ingest_active":
                        defs.append(f"{path.name}:{node.lineno}")
    assert defs == ["runtime_config.py:" + str(
        next(n.lineno for n in ast.walk(_tree(APP / "runtime_config.py"))
             if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "_startup_ingest_active"
                     for t in n.targets))
    )], f"expected exactly one definition, in runtime_config: {defs}"
