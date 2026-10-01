"""The auth middleware decides on the ROUTER's path (2026-09-30).

request.url.path is rebuilt from the Host header: with "Host: x/api/health?" it
reads /api/health - an exempt path - while the router serves the real one.
The middleware used to decide its exemptions on that value, so a crafted Host
header could walk any request past it; the route's own guard was then the only
lock. It now reads scope["path"], the string the router matches on. The route
below has no guard of its own, so only the middleware stands in front of it.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.auth as auth


def _mini(monkeypatch):
    monkeypatch.setattr(auth, "ENABLE_AUTH", True)
    mini = FastAPI()
    mini.add_middleware(auth.AuthMiddleware)

    @mini.get("/api/secret")
    def secret():
        return {"ok": True}

    @mini.get("/api/health")
    def health():
        return {"status": "ok"}

    return TestClient(mini)


def test_a_crafted_host_header_cannot_steer_the_exemption(monkeypatch):
    c = _mini(monkeypatch)
    for host in ("x/api/health?", "x/api/health#", "x/api/health?a=b"):
        r = c.get("/api/secret", headers={"host": host})
        assert r.status_code == 401, (host, r.status_code)


def test_an_exempt_path_is_still_exempt_and_a_plain_request_still_refused(monkeypatch):
    c = _mini(monkeypatch)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/secret").status_code == 401


def test_the_predicate_is_one_function():
    assert auth.is_excluded("/api/health")
    assert not auth.is_excluded("/api/secret")


def test_a_typed_token_is_not_a_session(monkeypatch):
    """An MFA challenge or a sign-in handoff shares the signing secret but is not
    a session; the middleware refuses it (2026-09-30) - it used to let one past,
    leaving each route's own dependency to refuse it."""
    from datetime import datetime, timedelta, timezone
    from jose import jwt
    c = _mini(monkeypatch)
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)

    def bearer(claims):
        tok = jwt.encode({"sub": "1", "role": "admin", "exp": exp, **claims},
                         auth.SECRET_KEY, algorithm=auth.ALGORITHM)
        return {"Authorization": "Bearer " + tok}

    for typed in ("mfa", "sso_handoff", "anything"):
        assert c.get("/api/secret", headers=bearer({"type": typed})).status_code == 401, typed
    assert c.get("/api/secret", headers=bearer({})).status_code == 200
