"""Two structural rules the router split depends on, enforced by machine.

Both were stated in docstrings and commit messages first, and both had already
slipped once by the end of the first router extraction - which is the argument
for checking them here instead of remembering them nine more times.

There is no linter in CI (the four jobs are residue, gitleaks, tests, image),
so these run in the suite.
"""
import ast
import pathlib
import re

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


# ── Defined and never called ─────────────────────────────────────────────────
#
# A module-level function that nothing outside the tests reaches is invisible
# to every passing test and to CI: a test that calls a function directly
# proves it works, not that the product uses it. This repo shipped that shape
# at least three times: check_daily_guest_budget (the guest spend bound, found
# 2026-08-26), clear_trust_cache (2026-08-28), and resolve_moot_holds, found
# when this check first ran (2026-10-03) - with no caller, a held upload
# outlived its clean re-upload, and releasing it put the old text back.
#
# Same contract as the dead-import check above: every exemption states why the
# function stays with no caller, and an exemption that stops being needed
# fails, so the list cannot outlive its reasons. A test-only hook is exempt by
# its name (`*_for_tests`) rather than by an entry.

# The directories the production image ships (backend/Dockerfile): a caller
# counts only if it ships, so the check reads the same files here and in the
# image. alembic/ is read where a surface has one.
_SHIPPED = ("app", "scripts", "alembic")

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_ALLOWED_UNCALLED = {
    "app/crypto_at_rest.py:decrypt_at_rest":
        "the strict reader, kept for this module's next tenant - the one "
        "tolerant caller uses try_decrypt_at_rest (see that docstring)",
    "app/jwt_auth.py:unusable_password_hash":
        "the no-password sentinel an SSO door stamps; this surface has no SSO "
        "yet and the rule rides so a port inherits it (the comment above "
        "UNUSABLE_PASSWORD_PREFIX)",
    "app/pii.py:redact_output":
        "the one-shot oracle the streaming OutputFilter is property-tested "
        "against (test_output_pii.py)",
    "app/state_store.py:put_if_absent":
        "the single-use primitive an SSO door burns its one-time state with; "
        "this surface has no SSO door yet",
}


def _shipped_modules():
    for d in _SHIPPED:
        base = APP.parent / d
        if base.is_dir():
            yield from sorted(p for p in base.rglob("*.py")
                              if "__pycache__" not in p.parts)


def _docstrings(tree):
    """The string nodes that are docstrings: a docstring that names a function
    describes it, it does not reach it."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                found.add(id(body[0].value))
    return found


def _reached(tree):
    """Names one module reaches a function by, and its import aliases.

    A read by name, an attribute (`module.fn`), and an identifier-shaped word
    in a non-docstring string - getattr and registry dispatch reach a function
    that way. A reference inside the function's own body (recursion) does not
    count: it reaches nothing from outside."""
    reached, aliases = set(), {}
    docs = _docstrings(tree)
    for stmt in tree.body:
        owner = (stmt.name if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                 else None)
        for node in ast.walk(stmt):
            if isinstance(node, ast.alias):
                if node.asname:
                    aliases.setdefault(node.asname, set()).add(node.name.rsplit(".", 1)[-1])
                continue
            if isinstance(node, ast.Name):
                words = [node.id]
            elif isinstance(node, ast.Attribute):
                words = [node.attr]
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and id(node) not in docs):
                words = _WORD.findall(node.value)
            else:
                continue
            reached.update(w for w in words if w != owner)
    return reached, aliases


def _uncalled_functions():
    """(key, line) for every undecorated module-level function under app/ that
    nothing shipped reaches. A decorator registers its function somewhere (a
    route, a hook), so a decorated one is reached by construction."""
    reached, aliases = set(), {}
    for path in _shipped_modules():
        r, a = _reached(_tree(path))
        reached |= r
        for alias, originals in a.items():
            aliases.setdefault(alias, set()).update(originals)
    for alias, originals in aliases.items():
        if alias in reached:          # `import fire as fire_alert`, then fire_alert()
            reached |= originals
    for path in _modules():
        for node in _tree(path).body:
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not node.decorator_list
                    and not node.name.startswith("__")
                    and not node.name.endswith("_for_tests")
                    and node.name not in reached):
                yield f"{path.relative_to(APP.parent).as_posix()}:{node.name}", node.lineno


def test_no_module_level_function_is_defined_and_never_called():
    uncalled, exemptions_used = [], set()
    for key, lineno in _uncalled_functions():
        if key in _ALLOWED_UNCALLED:
            exemptions_used.add(key)
            continue
        uncalled.append(f"{key} (line {lineno})")
    assert not uncalled, (
        "module-level functions nothing outside the tests calls. Wire the "
        "caller, delete the function, or add it to _ALLOWED_UNCALLED with the "
        "reason it stays:\n  " + "\n  ".join(sorted(uncalled)))
    stale = sorted(set(_ALLOWED_UNCALLED) - exemptions_used)
    assert not stale, f"_ALLOWED_UNCALLED entries no longer needed - remove them: {stale}"
