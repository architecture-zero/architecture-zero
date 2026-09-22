"""The request-body ceiling BEFORE the JSON parser (app/body_limit.py).

The chat route's character bound runs inside the handler, after FastAPI has
read and parsed the whole body, so it bounds what reaches the provider and
nothing else. The routes a caller reaches before signing in (chat, login,
refresh, setup, MFA completion) would otherwise hand a 60 MB body to
json.loads - and the shipped compose publishes the backend port directly, so
the proxy's own ceiling only ever covered one door. These pin the ceiling at
the app, the two ingest exemptions, and the chunked path.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.body_limit import BodySizeLimit
from app.runtime_config import MAX_JSON_BODY_BYTES


def _installed():
    from app.main import app
    return next(m for m in app.user_middleware if m.cls is BodySizeLimit)


def test_a_declared_oversize_body_is_refused_before_the_handler(client):
    body = b'{"prompt": "' + b"x" * (MAX_JSON_BODY_BYTES + 16) + b'"}'
    with patch("app.routers.chat.check_injection") as scan:
        r = client.post("/api/chat", content=body,
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert "Request body too large" in r.json()["detail"]
    scan.assert_not_called()


def test_the_ceiling_is_wired_with_the_two_ingest_exemptions():
    m = _installed()
    assert m.kwargs["default_limit"] == MAX_JSON_BODY_BYTES
    per_path = m.kwargs["per_path"]
    assert per_path["/api/ingest/upload"] is None          # streams and stops at MAX_UPLOAD_MB itself
    assert per_path["/api/ingest"] >= 50 * 1024 * 1024     # the JSON text-ingest door, MAX_UPLOAD_MB
    inst = BodySizeLimit(lambda *a: None, default_limit=m.kwargs["default_limit"], per_path=per_path)
    assert inst.limit_for("/api/chat") == MAX_JSON_BODY_BYTES
    assert inst.limit_for("/api/auth/login") == MAX_JSON_BODY_BYTES
    assert inst.limit_for("/api/ingest/upload") is None


def test_the_ceiling_sits_inside_the_auth_middleware():
    """Innermost: a bearer-less request on a non-excluded route is refused by
    AuthMiddleware before any body is read; the ceiling covers the excluded
    routes, which are the ones that take a body from anyone."""
    from app.main import app
    from app.auth import AuthMiddleware
    names = [m.cls for m in app.user_middleware]
    assert names.index(AuthMiddleware) < names.index(BodySizeLimit)


def test_a_chunked_body_is_refused_as_it_crosses_the_ceiling():
    """No Content-Length: counted as it streams, refused the moment it crosses."""
    seen = []

    async def inner(scope, receive, send):
        while True:
            message = await receive()
            seen.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                break

    mw = BodySizeLimit(inner, default_limit=100, per_path={})
    chunks = [b"a" * 60, b"b" * 60]

    async def receive():
        body = chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message):
        pass

    with pytest.raises(HTTPException) as e:
        asyncio.run(mw({"type": "http", "path": "/api/chat", "headers": []}, receive, send))
    assert e.value.status_code == 413
    assert seen == [60]            # the first chunk reached the app, the second never did


def test_zero_disables_the_ceiling():
    async def inner(scope, receive, send):
        await receive()

    mw = BodySizeLimit(inner, default_limit=0, per_path={})

    async def receive():
        return {"type": "http.request", "body": b"x" * 10_000, "more_body": False}

    async def send(message):
        pass

    asyncio.run(mw({"type": "http", "path": "/api/chat",
                    "headers": [(b"content-length", b"10000")]}, receive, send))
