"""Auth middleware, the fail-closed boot guard, and peer-key scopes.

The middleware is the OUTER layer only. The real enforcement is route-level:
every non-public route carries Depends(get_current_user), and a wiring test
sweeps app.routes to keep that true - so authorization holds even with
ENABLE_AUTH=false (the test suite's own mode, and the reason a
middleware-only gate is invisible to tests).
"""
import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware

# Private by default: the middleware layer is ON unless deliberately turned
# off for a public-demo posture (route-level auth still holds either way).
ENABLE_AUTH = os.getenv("ENABLE_AUTH", "true").lower() == "true"
SECRET_KEY  = os.getenv("JWT_SECRET_KEY", "change-me-before-deploying")

# Fail closed, UNCONDITIONALLY: route-level dependencies validate JWTs
# whether or not the middleware layer is on, so a missing/placeholder secret
# means every token is signed with a world-known key and anyone can forge an
# owner token - auth "off" does not make the secret unused. Refuse to boot
# rather than ship that.
if SECRET_KEY in ("", "change-me-before-deploying"):
    raise RuntimeError(
        "SECURITY: JWT_SECRET_KEY is unset or the default placeholder - "
        "tokens would be forgeable. Set a strong secret first: "
        'python -c "import secrets; print(secrets.token_hex(32))"'
    )

ALGORITHM       = "HS256"
# Service token for the file watcher's ingest calls - a machine identity that
# never expires like a user JWT would mid-ingest.
WATCHER_API_KEY = os.getenv("WATCHER_API_KEY", "")

# ── Peer federation keys (per-caller scope) ──────────────────────────────────
# PEER_KEYS = comma-separated <key>:<scope> pairs. A scope is a CLEARANCE rung
# (permissions.PEER_SCOPE_LEVELS), and the serve route filters departments by
# the same department_min_level() the rest of retrieval uses:
#   public -> the global KB only; a ?department= request is ignored
#   all    -> global, plus departments the operator shared at or below Admin
#   owner  -> everything, internal/Owner-only departments included
# `all` no longer means "every department". It meant that until v0.1.1, which
# reached `restricted`, `history` and every unlisted (fail-closed Owner-only)
# department - so the friendly-sounding word shipped an operator's internal
# docs off-box. Serving those now requires typing `owner`, deliberately.
# An unrecognized scope grants nothing (the route 403s).
# One key per caller, so a leaked key revokes one peer, not the federation.
# Plain text (no JSON/quotes) so docker compose's .env parser accepts it.
ECO_EXPOSE_KB = os.getenv("ECO_EXPOSE_KB", "false").lower() == "true"


def _load_peer_key_scopes() -> dict:
    scopes: dict[str, str] = {}
    for entry in os.getenv("PEER_KEYS", "").split(","):
        key, _, scope = entry.strip().partition(":")
        scope = scope.strip()
        if key and scope:
            scopes[key] = scope
    return scopes


PEER_KEY_SCOPES = _load_peer_key_scopes()

# Paths the middleware never gates. Deliberately SHORT: only auth bootstrap
# (no token exists yet), liveness and build identity, the guest-gated chat
# endpoint (its gate is internal and double-latched), the public trust panel
# (read-only, derived - the point is that visitors see it), the backup-status
# prober (no JWT; its 503 IS the alarm), and /metrics (gated by its own
# route-level auth dependency). Everything else authenticates at the route
# level regardless of this list.
EXCLUDED_PATHS = {
    "/",
    "/api/health",
    "/api/health/ready",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/setup",
    "/api/auth/needs-setup",
    "/api/auth/config",
    "/api/auth/mfa/complete",
    "/api/chat",
    # In-product help (2026-09-21): the help page behind a citation chip,
    # self-gated in the handler EXACTLY like /api/chat (a signed-in account,
    # or a guest where the guest door is open). Without this line a guest on
    # an ENABLE_AUTH=true instance could hold a help conversation and get a
    # middleware 401 on every citation.
    "/api/help/page",
    "/api/trust",
    "/api/version",
    "/api/backup-status",
    "/metrics",
}


# The longest Authorization header the credential check will look at. A token
# this instance signs is a few hundred characters, and the check runs before
# any authentication - it must not be handed megabytes to decode.
MAX_CREDENTIAL_CHARS = 4096


def presents_a_credential(authorization: str) -> bool:
    """Whether an Authorization header carries something this instance
    accepts: the watcher's service key, or an unexpired session token it
    signed.

    The body ceiling (app/body_limit.py) asks this before it lets a caller
    through a DOOR - a path that takes a document, and so carries a wider
    figure than the default - and it asks whatever ENABLE_AUTH says. With
    auth off the middleware below admits everyone, and the routes that take
    a document answer 401 from a dependency - which FastAPI resolves only
    AFTER it has read and parsed the body. So "who may send 50 MB" cannot be
    left to the route. It says nothing about what the caller may DO: the
    route's own guard still decides that, after the body is in."""
    if len(authorization) > MAX_CREDENTIAL_CHARS or not authorization.startswith("Bearer "):
        return False
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return False
    if WATCHER_API_KEY and hmac.compare_digest(token.encode("utf-8"),
                                               WATCHER_API_KEY.encode("utf-8")):
        return True
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return False
    # A TYPED token is a step on the way to a session - the second-factor
    # challenge, a sign-in handoff - not a session, and one with no expiry is
    # not one this instance mints. Every place that reads a token for who the
    # caller IS refuses both, and so does this.
    return payload.get("type") is None and payload.get("exp") is not None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Peer KB gate - enforced regardless of ENABLE_AUTH, so a dev
        # instance with auth off still cannot leak its KB to an unkeyed peer.
        if ECO_EXPOSE_KB and request.url.path == "/api/query-kb":
            peer_key = request.headers.get("X-Peer-Key", "")
            scope = PEER_KEY_SCOPES.get(peer_key) if peer_key else None
            if scope:
                request.state.peer_scope = scope
                return await call_next(request)
            return JSONResponse(status_code=401, content={"detail": "Invalid peer key"})

        if not ENABLE_AUTH:
            return await call_next(request)

        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header.removeprefix("Bearer ").strip()

        # Constant-time, as the credential check above already was (since
        # 2026-09-30): this compared with ==, which returns at the first
        # differing byte.
        if WATCHER_API_KEY and hmac.compare_digest(token.encode("utf-8"),
                                                   WATCHER_API_KEY.encode("utf-8")):
            request.state.user_id = 0
            request.state.role = "service"
            return await call_next(request)

        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            request.state.user_id = int(payload.get("sub", 0))
            request.state.role = payload.get("role", "member")
        except JWTError:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        return await call_next(request)
