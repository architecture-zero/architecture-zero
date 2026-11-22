"""The app's route table, walked the way the router matches it (2026-11-22).

FastAPI 0.137 stopped copying an included router's routes into the app:
app.routes now holds each include_router as one node, with its routes under
it. A sweep that reads app.routes and keeps the APIRoute entries saw 100 of
this app's 100 path operations on fastapi 0.115.5 and 0 of them on 0.162.7 -
and a sweep that finds nothing to check passes. iter_route_contexts is
FastAPI's public walker for that tree; each item it yields answers path,
methods and dependant as a request sees them (the full path, include-level
dependencies merged in).

test_route_table.py pins the walk to the source: the number of path
operations it yields must equal the number the app's code declares, so a
framework change that hides routes from it fails there, loudly, instead of
emptying every sweep built on it.
"""
from fastapi.routing import APIRoute, iter_route_contexts


def api_routes(app):
    """Every path operation `app` serves, in registration order - the order
    the router tries them."""
    return [r for r in iter_route_contexts(app.routes)
            if isinstance(r.original_route, APIRoute)]


def flat_params(dependant, kind):
    """Every parameter of `kind` ("path_params", "query_params" or
    "body_params") a route takes, its dependencies' included: the route's own
    first, then each dependency's in declaration order, depth first, repeats
    kept. Walked over Dependant's own fields - FastAPI's get_flat_dependant
    did this until 0.140 removed it, and what replaced it is private."""
    out, stack = [], [dependant]
    while stack:
        d = stack.pop()
        out.extend(getattr(d, kind))
        stack.extend(reversed(d.dependencies))
    return out
