"""A request-body ceiling that runs BEFORE any parser (2026-09-21; the upload
door and the credential check joined it 2026-09-28).

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

Ceilings by path: every route gets MAX_JSON_BODY_BYTES (2 MB by default -
CHAT_MAX_INPUT_CHARS' 200,000 characters is under 800 KB even as raw
four-byte UTF-8; a client that escapes them instead sends twelve bytes each
and meets this ceiling first, which refuses it all the same). The two ingest
doors exist to take a document, so they carry MAX_UPLOAD_MB instead: POST
/api/ingest as JSON text, POST /api/ingest/upload as a multipart file with
room for its envelope.

THE UPLOAD DOOR WAS EXEMPT HERE until 2026-09-28, on the reasoning that its
handler reads the file in chunks and stops AT its limit. It does - and that
bounds the handler's buffer, not the bytes taken in. FastAPI parses a
multipart form before the handler or any permission dependency runs, and the
parser spools a file part to disk with no size limit of its own: measured, an
80 MB body was taken off the wire in full before the handler answered 413 for
a 50 MB limit. A None override still means "no ceiling here", and nothing
shipped uses it - a route is exempt only if something BEFORE its parser
bounds what it takes in.

A WIDER FIGURE IS FOR A CALLER WITH A CREDENTIAL. A path whose own figure is
wider than the default is a DOOR: it exists to take a document, and every
door's route asks who is calling. It asks from a dependency, though, and
FastAPI resolves a dependency only after it has read and parsed the body -
so on an instance that runs with auth off, where the auth middleware admits
everyone, "who may send 50 MB" was anyone at all. It is decided here
instead: `wider_for` is asked, with the request's Authorization header, and
a caller who presents nothing is answered 401 on the headers, before a byte
is read. The route would have said the same, after the body was in. (Holding
that caller to the default figure instead was the first version, and it let
them IN: behind a proxy that streams the doors, 300 stalled uploads were 300
requests held inside the app.) A check that raises grants nothing.

WHAT ZERO MEANS. A default of 0 switches the default ceiling OFF, by the
operator's choice; the paths with a figure of their own keep it, for every
caller, because nothing is wider than no ceiling at all. A path's own figure
of zero or less is a setting gone wrong (an upload size of 0 MB), never an
exemption - None is the one way to ask for that - so the path keeps the
default, and the startup line says so. That line names every figure in force.

Every refusal is counted and logged with figures only - the path, the size,
the ceiling - never a byte of the body. Pure ASGI, no BaseHTTPMiddleware: it
must see the raw receive channel to count.
"""
import json

from fastapi import HTTPException

from app.logger import log
from app.metrics import increment


def _detail(size: int, limit: int) -> str:
    return (f"Request body too large: {size:,} bytes, and this route accepts up to "
            f"{limit:,}. Send less at a time.")


def _bounds(figure) -> bool:
    """Whether a figure bounds anything: a positive number does. None and
    zero do not."""
    return isinstance(figure, int) and not isinstance(figure, bool) and figure > 0


def _record(path: str, size: int, limit: int, declared: bool) -> None:
    """Count and log one refusal. `declared` says which leg refused it: the
    Content-Length the caller announced, or the count of what arrived. The
    path is the caller's to choose, so it is cut before it is written."""
    increment("request_body_refused_total")
    log("request_body_refused", path=path[:200], size=size, limit=limit,
        declared=declared)


async def _answer(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii"))]})
    await send({"type": "http.response.body", "body": body})


class BodySizeLimit:
    """ASGI middleware. `per_path` overrides the default ceiling for exact
    paths; a None override means "no ceiling here" (something before the
    route's parser bounds what it takes in). `wider_for(authorization)` says
    whether this caller may use a path whose figure is WIDER than the default."""

    def __init__(self, app, default_limit: int, per_path: dict | None = None,
                 wider_for=None):
        self.app = app
        self.default_limit = int(default_limit)
        self.wider_for = wider_for
        self.per_path = {}
        ignored = {}
        for path, figure in (per_path or {}).items():
            if figure is None or _bounds(figure):
                self.per_path[path] = figure
            else:
                ignored[path] = figure
        log("request_body_ceiling",
            default=self.default_limit if _bounds(self.default_limit) else "off",
            doors={path: ("none" if figure is None else figure)
                   for path, figure in self.per_path.items()},
            ignored=ignored, credential_check=wider_for is not None)

    def _admits(self, authorization: str) -> bool:
        if self.wider_for is None:
            return True
        try:
            return bool(self.wider_for(authorization))
        except Exception:
            return False

    def is_door(self, path: str) -> bool:
        """A path whose own figure is wider than the default. With the default
        switched off nothing is wider than it, so nothing is a door."""
        if path not in self.per_path or not _bounds(self.default_limit):
            return False
        own = self.per_path[path]
        return own is None or own > self.default_limit

    def limit_for(self, path: str, authorization: str = "") -> int | None:
        """The ceiling this caller would meet on this path."""
        if path not in self.per_path:
            return self.default_limit
        if self.is_door(path) and not self._admits(authorization):
            return self.default_limit
        return self.per_path[path]

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        declared = None
        authorization = ""
        for name, value in scope.get("headers") or []:
            if name == b"content-length" and declared is None:
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
            elif name == b"authorization" and not authorization:
                authorization = value.decode("latin-1")
        if self.is_door(path) and not self._admits(authorization):
            increment("document_door_refused_total")
            log("document_door_refused", path=path[:200])
            await _answer(send, 401, "Not authenticated")
            return
        limit = self.limit_for(path, authorization)
        if not _bounds(limit):
            return await self.app(scope, receive, send)

        if declared is not None and declared > limit:
            _record(path, declared, limit, declared=True)
            await _answer(send, 413, _detail(declared, limit))
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    _record(path, received, limit, declared=False)
                    raise HTTPException(status_code=413, detail=_detail(received, limit))
            return message

        return await self.app(scope, limited_receive, send)
