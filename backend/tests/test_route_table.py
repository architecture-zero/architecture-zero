"""The route walk every sweep stands on sees every route (2026-11-22).

The authorization sweep, the no-500 sweep and the missing-account sweep each
check the routes tests/route_table.py hands them, and each passes when it is
handed none. On fastapi 0.162.7 the walk they used before read 0 of this
app's 100 path operations. This file is what makes a short walk fail: the
walk must yield exactly as many path operations as the app's code declares.
"""
import ast
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI

from app.main import app
from tests.route_table import api_routes, flat_params

APP_DIR = Path(__file__).resolve().parent.parent / "app"
VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route"}


def _declared_path_operations():
    """Path operations declared in app/: each @router.<verb>(...) or
    @app.<verb>(...) decorator, and each add_api_route(...) call."""
    found = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in VERBS and isinstance(d.func.value, ast.Name)
                            and d.func.value.id in {"router", "app"}):
                        found.append(f"{path.name}:{node.lineno} {node.name}")
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_api_route"):
                found.append(f"{path.name}:{node.lineno} add_api_route")
    return found


def test_the_walk_yields_every_path_operation_the_code_declares():
    declared = _declared_path_operations()
    walked = api_routes(app)
    assert len(declared) >= 90, declared  # the floor: this app declared 100 on 2026-11-22
    assert len(walked) == len(declared), (
        f"the route walk yields {len(walked)} path operations and app/ declares "
        f"{len(declared)} - every sweep built on tests/route_table.py checks only "
        "what the walk yields")


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
    probe.include_router(router, prefix="/outer", dependencies=[])
    probe.include_router(router, prefix="/guarded",
                         dependencies=[Depends(guard)])
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
