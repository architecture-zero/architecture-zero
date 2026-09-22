"""A request-body ceiling that runs BEFORE the JSON parser (2026-09-21).

FastAPI reads and parses the whole body before a handler's first line runs,
so every bound inside a handler - the chat route's character cap included -
is a bound on what reaches the PROVIDER, never on what reaches json.loads.
The middleware-excluded routes (chat, login, refresh, setup, the MFA
completion) take a body from anyone, and the shipped compose publishes the
backend port directly, so the proxy's client_max_body_size only ever covered
one of the two doors. This covers both.

A Content-Length over the ceiling is refused with a plain 413 before a byte
of the body is read. A body that arrives without one (chunked) is counted as
it streams and refused the moment it crosses the ceiling - FastAPI re-raises
an HTTPException raised from inside its body read, so the caller still gets
the 413 rather than a closed socket.

Two ceilings, by path: every route gets MAX_JSON_BODY_BYTES (2 MB by default -
CHAT_MAX_INPUT_CHARS' 200,000 characters is under 800 KB even as four-byte
UTF-8); POST /api/ingest, the JSON text-ingest door, gets MAX_UPLOAD_MB like
its multipart sibling; POST /api/ingest/upload is exempt because it reads
its own stream in chunks and stops AT its limit already. Pure ASGI, no
BaseHTTPMiddleware: it must see the raw receive channel to count.
"""
import json

from fastapi import HTTPException


def _detail(size: int, limit: int) -> str:
    return (f"Request body too large: {size:,} bytes, and this route accepts up to "
            f"{limit:,}. Send less at a time.")


async def _refuse(send, size: int, limit: int) -> None:
    body = json.dumps({"detail": _detail(size, limit)}).encode("utf-8")
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii"))]})
    await send({"type": "http.response.body", "body": body})


class BodySizeLimit:
    """ASGI middleware. `per_path` overrides the default ceiling for exact
    paths; a None override means "no ceiling here" (the route bounds its own
    stream)."""

    def __init__(self, app, default_limit: int, per_path: dict | None = None):
        self.app = app
        self.default_limit = int(default_limit)
        self.per_path = dict(per_path or {})

    def limit_for(self, path: str) -> int | None:
        if path in self.per_path:
            return self.per_path[path]
        return self.default_limit

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope.get("path", ""))
        if not limit or limit <= 0:
            return await self.app(scope, receive, send)

        declared = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            await _refuse(send, declared, limit)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise HTTPException(status_code=413, detail=_detail(received, limit))
            return message

        return await self.app(scope, limited_receive, send)
