"""Auth middleware, the fail-closed boot guard, and peer-key scopes.

The middleware is the OUTER layer only. The real enforcement is route-level:
every non-public route carries Depends(get_current_user), and a wiring test
sweeps app.routes to keep that true - so authorization holds even with
ENABLE_AUTH=false (the test suite's own mode, and the reason a
middleware-only gate is invisible to tests).
"""
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
# PEER_KEYS = comma-separated <key>:<scope> pairs, scope in {"all", "public"}:
#   all    -> may read the global KB plus every private department
#   public -> global/public KB only; a ?department= request is ignored
# One key per caller, so a leaked key revokes one peer, not the federation.
# Plain text (no JSON/quotes) so docker compose's .env parser accepts it.
ECO_EXPOSE_KB = os.getenv("ECO_EXPOSE_KB", "false").lower() == "true"


def _load_peer_key_scopes() -> dict:
    scopes: dict[str, str] = {}
    for entry in os.getenv("PEER_KEYS", "").split(","):
        key, _, scope = entry.strip().partition(":")
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
    "/api/trust",
    "/api/version",
    "/api/backup-status",
    "/metrics",
}


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

        if WATCHER_API_KEY and token == WATCHER_API_KEY:
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
