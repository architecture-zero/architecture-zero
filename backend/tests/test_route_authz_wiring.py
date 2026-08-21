"""Route-level auth on EVERY route, or a deliberate entry in the public list.

Middleware-only auth is the dangerous class: the suite runs ENABLE_AUTH=false,
so a route that relies on AuthMiddleware alone is invisible to tests - and an
instance deployed with auth off is an open house. Every route that is not
deliberately anonymous carries route-level auth (Depends(get_current_user)
directly, or via require_owner / require_permission); THIS test is what keeps
that true.

Two-sided on purpose: a new route without auth fails loudly (add the
dependency, or make the public choice explicit here), and a stale allowlist
entry fails too (a route that BECAME protected must leave the list, or the
list rots into fiction). Every entry below states why it is public.
"""
from fastapi.routing import APIRoute

from app.jwt_auth import get_current_user
from app.main import app

# (METHOD, path) pairs that are route-level anonymous BY DESIGN. Each carries
# its own gate where one is needed - stated per entry.
PUBLIC_BY_DESIGN = {
    # Auth bootstrap: no token exists yet by definition.
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/mfa/complete"),
    ("POST", "/api/auth/refresh"),
    ("GET", "/api/auth/needs-setup"),
    ("GET", "/api/auth/config"),
    ("POST", "/api/auth/setup"),          # refuses once an Owner exists
    # Peer federation serve: gated by X-Peer-Key middleware when
    # ECO_EXPOSE_KB=true, and the route itself 403s without a peer scope
    # stamp - sealed even with the middleware off (tested below).
    ("GET", "/api/query-kb"),
    # Liveness + version: monitoring probes carry no JWT.
    ("GET", "/"),
    ("GET", "/api/version"),
    ("GET", "/api/health"),
    ("GET", "/api/health/ready"),
    ("GET", "/api/backup-status"),        # uptime checks probe it; body discloses only ok/age/reason
    # Chat: guest access is double-gated inside (ALLOW_GUEST_MODE env AND
    # admin config), resolved via optional_user - not get_current_user.
    ("POST", "/api/chat"),
    # Public trust panel: read-only, derived from stored eval rows - the
    # point is that visitors see it. The operator variant (/api/admin/trust)
    # authenticates route-level.
    ("GET", "/api/trust"),
}
# NOTE: /metrics sits in the middleware's EXCLUDED_PATHS but is NOT public -
# it carries route-level Depends(get_current_user), so it must never appear
# in this list (the stale-entry side below would catch it if it did).


def _dep_calls(dependant, acc):
    for d in dependant.dependencies:
        call = getattr(d, "call", None)
        if call is not None:
            acc.add(call)
        _dep_calls(d, acc)


def _route_protected(route) -> bool:
    acc = set()
    _dep_calls(route.dependant, acc)
    return get_current_user in acc


def test_every_route_is_protected_or_deliberately_public():
    unprotected = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue  # docs/openapi (starlette Routes) and websockets
        if not _route_protected(route):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                unprotected.add((method, route.path))

    missing = unprotected - PUBLIC_BY_DESIGN
    stale = PUBLIC_BY_DESIGN - unprotected
    assert not missing, (
        "routes with NO route-level auth and no deliberate public entry "
        f"(add Depends(get_current_user) or an entry with a stated reason): {sorted(missing)}")
    assert not stale, (
        "PUBLIC_BY_DESIGN entries that are now protected or gone - prune so "
        f"the list stays true: {sorted(stale)}")


def test_a_protected_route_actually_401s_without_a_token(client):
    """The sweep's claim proven end-to-end on representatives across the
    surface - middleware is OFF in the suite, so any 401 here is the
    ROUTE-level dependency doing the work."""
    for path in ("/api/status", "/metrics", "/api/config", "/api/models",
                 "/api/sessions", "/api/analytics", "/api/users",
                 "/api/settings", "/api/peers", "/api/health/detailed",
                 "/api/admin/trust", "/api/history/default"):
        assert client.get(path).status_code == 401, path


def test_protected_routes_pass_with_auth(client, admin_headers):
    for path in ("/api/sessions", "/api/analytics", "/api/status",
                 "/api/config"):
        assert client.get(path, headers=admin_headers).status_code == 200, path


def test_query_kb_fails_closed_without_a_peer_scope(client):
    """The peer-serve route must be sealed at the ROUTE level too: with the
    peer-key middleware off (this suite's mode), no scope stamp exists and
    the route refuses - defense in depth, same posture as every other
    public-by-design entry documenting its own gate."""
    assert client.get("/api/query-kb", params={"q": "anything"}).status_code == 403
