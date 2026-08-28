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


# ── The privilege LEVEL, not just "is it protected" ──────────────────────────
#
# The sweep above answers a yes/no question: does get_current_user appear
# anywhere in the dependency tree? require_owner and require_permission(scope)
# BOTH declare `current_user: dict = Depends(get_current_user)`, so owner-only,
# scoped, and any-authenticated are one indistinguishable state to it - and the
# scope string lives in a closure cell, so even swapping "manage_system" for
# "chat" is invisible.
#
# That gap was measured, not supposed: downgrading /api/health/detailed from
# require_owner and /api/admin/trust from require_permission("view_analytics")
# to a bare get_current_user left the whole suite at its exact baseline, 447
# passed / 1 skipped, while any Member-role token could then read disk usage,
# DB latency, which provider keys are present, and drive outbound alert
# webhooks by polling.
#
# Guard REMOVAL was already caught loudly. Guard DOWNGRADE was caught by
# nothing - which matters because main.py is being split into routers and every
# one of these guards is being carried by hand from one file to another.
#
# Two-sided like PUBLIC_BY_DESIGN: a route that weakens fails, a route that
# strengthens fails until its pin is updated deliberately, a new route fails
# until it is pinned, and a stale pin fails. Regenerating this dict wholesale
# to make it pass defeats the point - change the line you meant to change.
REQUIRED_GUARD = {
    ("GET", "/"): "public",
    ("GET", "/api/admin/audit"): "require_permission:view_analytics",
    ("GET", "/api/admin/audit/export"): "require_permission:view_analytics",
    ("POST", "/api/admin/backup"): "require_owner",
    ("GET", "/api/admin/backup/status"): "require_owner",
    ("GET", "/api/admin/config"): "require_permission:manage_system",
    ("PATCH", "/api/admin/config"): "require_permission:manage_system",
    ("GET", "/api/admin/context"): "require_permission:manage_system",
    ("PATCH", "/api/admin/context"): "require_permission:manage_system",
    ("DELETE", "/api/admin/evals/questions"): "require_owner",
    ("GET", "/api/admin/evals/questions"): "require_owner",
    ("POST", "/api/admin/evals/questions"): "require_owner",
    ("POST", "/api/admin/evals/questions/seed"): "require_owner",
    ("POST", "/api/admin/evals/questions/sync"): "require_owner",
    ("DELETE", "/api/admin/evals/questions/{question_id}"): "require_owner",
    ("PATCH", "/api/admin/evals/questions/{question_id}"): "require_owner",
    ("GET", "/api/admin/evals/recall"): "require_owner",
    ("PATCH", "/api/admin/evals/results/{result_id}"): "require_owner",
    ("POST", "/api/admin/evals/run"): "require_owner",
    ("GET", "/api/admin/evals/run-status/{run_id}"): "require_owner",
    ("GET", "/api/admin/evals/runs"): "require_owner",
    ("GET", "/api/admin/evals/runs/{run_id}"): "require_owner",
    ("GET", "/api/admin/injection-sources"): "require_permission:manage_kb",
    ("POST", "/api/admin/kb/prune-orphans"): "require_owner",
    ("GET", "/api/admin/kb/quarantine"): "require_permission:manage_kb",
    ("DELETE", "/api/admin/kb/quarantine/{item_id}"): "require_permission:manage_kb",
    ("POST", "/api/admin/kb/quarantine/{item_id}/release"): "require_permission:manage_kb",
    ("GET", "/api/admin/kb/rerank-status"): "require_owner",
    ("GET", "/api/admin/model-config"): "require_permission:manage_system",
    ("PATCH", "/api/admin/model-config"): "require_permission:manage_system",
    ("GET", "/api/admin/models"): "require_permission:manage_system",
    ("GET", "/api/admin/permissions"): "require_permission:manage_users",
    ("GET", "/api/admin/pii-sources"): "require_permission:manage_kb",
    ("GET", "/api/admin/trust"): "require_permission:view_analytics",
    ("POST", "/api/admin/users/{user_id}/mfa-reset"): "require_permission:manage_users",
    ("POST", "/api/admin/users/{user_id}/unlock"): "require_permission:manage_users",
    ("GET", "/api/analytics"): "require_permission:view_analytics",
    ("GET", "/api/auth/config"): "public",
    ("POST", "/api/auth/login"): "public",
    ("POST", "/api/auth/logout"): "get_current_user",
    ("GET", "/api/auth/me"): "get_current_user",
    ("PATCH", "/api/auth/me/password"): "get_current_user",
    ("PATCH", "/api/auth/me/username"): "get_current_user",
    ("POST", "/api/auth/mfa/complete"): "public",
    ("POST", "/api/auth/mfa/enable"): "get_current_user",
    ("POST", "/api/auth/mfa/setup"): "get_current_user",
    ("GET", "/api/auth/needs-setup"): "public",
    ("POST", "/api/auth/refresh"): "public",
    ("DELETE", "/api/auth/sessions"): "get_current_user",
    ("GET", "/api/auth/sessions"): "get_current_user",
    ("DELETE", "/api/auth/sessions/{token_id}"): "get_current_user",
    ("POST", "/api/auth/setup"): "public",
    ("GET", "/api/backup-status"): "public",
    ("POST", "/api/chat"): "optional_user",
    ("GET", "/api/config"): "get_current_user",
    ("POST", "/api/feedback"): "get_current_user",
    ("GET", "/api/feedback/summary"): "require_permission:view_analytics",
    ("GET", "/api/health"): "public",
    ("GET", "/api/health/detailed"): "require_owner",
    ("GET", "/api/health/ready"): "public",
    ("DELETE", "/api/history/{session_id}"): "get_current_user",
    ("GET", "/api/history/{session_id}"): "get_current_user",
    ("DELETE", "/api/history/{session_id}/tail"): "get_current_user",
    ("POST", "/api/ingest"): "require_permission:manage_kb",
    ("GET", "/api/ingest/departments"): "require_permission:manage_kb",
    ("DELETE", "/api/ingest/source/{source}"): "require_permission:manage_kb",
    ("GET", "/api/ingest/sources"): "require_permission:manage_kb",
    ("POST", "/api/ingest/upload"): "require_permission:manage_kb",
    ("GET", "/api/kb/files"): "require_permission:manage_kb",
    ("POST", "/api/kb/sync"): "require_permission:manage_kb",
    ("GET", "/api/models"): "get_current_user",
    ("GET", "/api/overview/metrics"): "require_permission:manage_system",
    ("GET", "/api/peers"): "require_owner",
    ("POST", "/api/peers"): "require_owner",
    ("GET", "/api/peers/status"): "require_owner",
    ("DELETE", "/api/peers/{peer_id}"): "require_owner",
    ("PATCH", "/api/peers/{peer_id}"): "require_owner",
    ("POST", "/api/peers/{peer_id}/reset-breaker"): "require_owner",
    ("GET", "/api/query-kb"): "public",
    ("GET", "/api/sessions"): "require_permission:view_analytics",
    ("POST", "/api/sessions"): "get_current_user",
    ("DELETE", "/api/sessions/{session_id}"): "get_current_user",
    ("PATCH", "/api/sessions/{session_id}"): "get_current_user",
    ("GET", "/api/settings"): "require_owner",
    ("PUT", "/api/settings"): "require_owner",
    ("GET", "/api/settings/test-ollama"): "require_owner",
    ("GET", "/api/status"): "get_current_user",
    ("GET", "/api/trust"): "public",
    ("GET", "/api/users"): "require_permission:manage_users",
    ("POST", "/api/users"): "require_permission:manage_users",
    ("DELETE", "/api/users/{user_id}"): "require_permission:manage_users",
    ("PATCH", "/api/users/{user_id}/department"): "require_permission:manage_users",
    ("PATCH", "/api/users/{user_id}/permissions"): "require_permission:manage_users",
    ("PATCH", "/api/users/{user_id}/role"): "require_permission:manage_users",
    ("GET", "/api/version"): "public",
    ("GET", "/metrics"): "get_current_user",
}

_RANK = {"require_owner": 3, "require_permission": 2, "get_current_user": 1,
         "optional_user": 0, "public": -1}


def _rank(ident: str) -> int:
    return _RANK.get(ident.split(":", 1)[0], -1)


def _dep_callables(dependant, acc):
    for d in dependant.dependencies:
        call = getattr(d, "call", None)
        if call is not None:
            acc.append(call)
        _dep_callables(d, acc)


def _permission_scope(fn):
    """require_permission(scope) returns a closure; the scope is only reachable
    through its cell, which is exactly why the yes/no sweep cannot see it."""
    code, cells = getattr(fn, "__code__", None), getattr(fn, "__closure__", None)
    if not code or not cells:
        return None
    for name, cell in zip(code.co_freevars, cells):
        if name == "scope":
            return cell.cell_contents
    return None


def _guard_identity(route) -> str:
    """The STRONGEST guard on the route, rendered as a comparable string."""
    acc = []
    _dep_callables(route.dependant, acc)
    best, best_rank = "public", -1
    for fn in acc:
        if "require_permission" in getattr(fn, "__qualname__", ""):
            ident, r = f"require_permission:{_permission_scope(fn)}", _RANK["require_permission"]
        elif getattr(fn, "__name__", "") in _RANK:
            ident, r = fn.__name__, _RANK[fn.__name__]
        else:
            continue
        if r > best_rank:
            best, best_rank = ident, r
    return best


def _actual_guards():
    out = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            out[(method, route.path)] = _guard_identity(route)
    return out


def test_no_route_silently_drops_to_a_weaker_privilege_level():
    """The one this exists for. A guard retyped at the wrong level during a
    route move is the most likely way authz breaks in a refactor, and it is
    invisible to every other test in the suite."""
    actual = _actual_guards()
    weakened = [
        (key, REQUIRED_GUARD[key], got)
        for key, got in actual.items()
        if key in REQUIRED_GUARD and got != REQUIRED_GUARD[key]
        and _rank(got) < _rank(REQUIRED_GUARD[key])
    ]
    assert not weakened, (
        "PRIVILEGE DOWNGRADE - these routes now accept a weaker caller than "
        "pinned. If deliberate, change the pin in the same commit and say why:\n"
        + "\n".join(f"  {m} {p}: pinned {want!r}, got {got!r}"
                     for (m, p), want, got in weakened))


def test_no_route_silently_changes_privilege_level_at_all():
    """The other three directions: a strengthened guard, a swapped permission
    scope, a new unpinned route, a stale pin. None is dangerous the way a
    downgrade is, but each means the pin no longer describes the app."""
    actual = _actual_guards()
    changed = [(k, REQUIRED_GUARD[k], v) for k, v in actual.items()
               if k in REQUIRED_GUARD and v != REQUIRED_GUARD[k]]
    unpinned = sorted(set(actual) - set(REQUIRED_GUARD))
    stale = sorted(set(REQUIRED_GUARD) - set(actual))
    assert not changed, (
        "guard level changed without updating its pin:\n"
        + "\n".join(f"  {m} {p}: pinned {want!r}, got {got!r}"
                     for (m, p), want, got in changed))
    assert not unpinned, f"routes with no REQUIRED_GUARD pin: {unpinned}"
    assert not stale, f"REQUIRED_GUARD pins for routes that no longer exist: {stale}"


def test_the_pin_covers_every_route():
    """Cheap canary: the split moves routes between files, and a router that
    silently fails to register would shrink this number with nothing else in
    the suite noticing."""
    assert len(_actual_guards()) == 96, (
        f"expected 96 routes, found {len(_actual_guards())} - a router failed "
        "to register, or routes were added without updating this count")
