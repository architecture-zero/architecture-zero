"""The app's route table, walked the way the router matches it (2026-11-22).

FastAPI 0.137 stopped copying an included router's routes into the app:
app.routes now holds each include_router as one node, with its routes under
it. A sweep that reads app.routes and keeps the APIRoute entries saw every
path operation on fastapi 0.115.5 and none of them on 0.162.7 (the template
read 100 of 100, then 0) - and a sweep that finds nothing to check passes.
iter_route_contexts is FastAPI's public walker for that tree; each item it
yields answers path, methods, endpoint and dependant as a request sees them
(the full path, include-level dependencies merged in).

test_route_table.py pins the walk to the source: every path operation the
app's code declares must come out of it, so a framework change that hides
routes from it fails there, loudly, instead of emptying every sweep built on
it.
"""
from fastapi.routing import APIRoute, iter_route_contexts


def api_routes(app):
    """Every path operation `app` serves, in registration order - the order
    the router tries them."""
    return [r for r in iter_route_contexts(app.routes)
            if isinstance(r.original_route, APIRoute)]


def all_routes(app):
    """Every route `app` can match, the path operations and the rest (the
    docs pages, the OpenAPI schema, any mount)."""
    return list(iter_route_contexts(app.routes))


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
