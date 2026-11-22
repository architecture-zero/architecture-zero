"""The route walk every sweep stands on sees every route (2026-11-22).

The authorization sweep, the no-500 sweep and the missing-account sweep each
check the routes tests/route_table.py hands them, and each passes when it is
handed none. On fastapi 0.162.7 the walk they used before read none of the
app's path operations. This file is what makes a short walk fail: every path
operation the app's code declares must come out of the walk, matched by its
endpoint, and nothing may come out that the code does not declare. A route
declared inside an `if` (registered only behind a flag) may be absent; the
tests that own those flags say when it must be there.
"""
import ast
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI

from app.main import app
from tests.route_table import all_routes, api_routes, flat_params

BACKEND = Path(__file__).resolve().parent.parent
VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route"}


def _router_names(tree):
    """The names a module binds to an APIRouter(...) or FastAPI(...) - the
    objects its path-operation decorators hang on, whatever they are called
    (router, public_radio_router, app), the class imported under an alias
    (`from fastapi import APIRouter as _APIRouter`) included."""
    makers = {"APIRouter", "FastAPI"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("fastapi"):
            makers |= {a.asname for a in node.names if a.name in {"APIRouter", "FastAPI"} and a.asname}
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Call):
            called = getattr(value.func, "id", None) or getattr(value.func, "attr", None)
            if called in makers:
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def _declared():
    """(always, behind_a_flag): Counters of (module, function) for each
    <router>.<verb>(...) decorator in app/, where <router> is a name the module
    binds to an APIRouter or a FastAPI app - one per decorator, so a function
    serving two paths counts twice."""
    always, flagged = Counter(), Counter()
    for path in sorted((BACKEND / "app").rglob("*.py")):
        module = ".".join(path.relative_to(BACKEND).with_suffix("").parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        routers = _router_names(tree)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            n = sum(1 for d in node.decorator_list
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and d.func.attr in VERBS and isinstance(d.func.value, ast.Name)
                    and d.func.value.id in routers)
            if not n:
                continue
            up, behind_if = parents.get(node), False
            while up is not None:
                behind_if = behind_if or isinstance(up, ast.If)
                up = parents.get(up)
            (flagged if behind_if else always)[(module, node.name)] += n
    return always, flagged


def test_the_walk_yields_every_path_operation_the_code_declares():
    always, flagged = _declared()
    walked = Counter((r.endpoint.__module__, r.endpoint.__name__) for r in api_routes(app))
    assert sum(always.values()) >= 90, always  # the floor: a source read that finds little is broken
    missing = always - walked
    assert not missing, (
        f"declared in app/ but not in the route walk: {sorted(missing)} - every sweep "
        "built on tests/route_table.py checks only what the walk yields")
    undeclared = walked - always - flagged
    assert not undeclared, f"walked but not declared as a path operation in app/: {sorted(undeclared)}"


def test_all_routes_holds_the_path_operations_and_the_rest():
    paths = {r.path for r in all_routes(app)}
    assert {r.path for r in api_routes(app)} <= paths
    if app.openapi_url:  # None when the app is configured to serve no schema
        assert app.openapi_url in paths


def test_the_walk_reads_paths_and_dependencies_through_an_include():
    """A router included with a prefix and its own dependency: the walk must
    answer the full path and the merged dependency, as a request sees them."""
    def guard():
        return None

    router = APIRouter()

    @router.get("/inner")
    def inner():
        return {}

    probe = FastAPI()
    probe.include_router(router, prefix="/outer")
    probe.include_router(router, prefix="/guarded", dependencies=[Depends(guard)])
    got = {r.path: [d.call for d in r.dependant.dependencies] for r in api_routes(probe)}
    assert got == {"/outer/inner": [], "/guarded/inner": [guard]}, got


def test_flat_params_walks_dependencies_in_order():
    def needs_a(a: int):
        return a

    def needs_b(b: str, _a=Depends(needs_a)):
        return b

    probe = FastAPI()

    @probe.get("/p/{x}")
    def ep(x: int, c: int = 0, _b=Depends(needs_b)):
        return {}

    (route,) = api_routes(probe)
    assert [p.name for p in flat_params(route.dependant, "path_params")] == ["x"]
    assert [p.name for p in flat_params(route.dependant, "query_params")] == ["c", "b", "a"]
