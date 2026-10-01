"""The backend's own port bounds the request head (2026-10-01).

Under the parser uvicorn[standard] installs and prefers (httptools), this port
took a 50 MB Authorization header whole and handed it to the app (measured on
uvicorn 0.32.1: 200 OK after 6.9 s), and only the credential check bounded a
token before decoding it - the middleware and decode_access_token, which the
route guards and the chat route call, decoded whatever arrived. Three layers
now:

  - the image's command runs h11 with a 64 KiB bound on the request head, so
    the server refuses an oversized head before the app sees a byte. These
    tests start uvicorn with the flags the Dockerfile's CMD carries, so the
    bound is proven on the command that ships, not on a copy of it;
  - every reader of the header refuses a token longer than any this instance
    signs (auth.MAX_CREDENTIAL_CHARS) before decoding it;
  - the shipped compose publishes the backend on loopback only, so the
    client's nginx - which bounds headers and bodies too - is the one door.
"""
import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt

import app.auth as auth

BACKEND = Path(__file__).resolve().parents[1]
DOCKERFILE = BACKEND / "Dockerfile"
COMPOSE = BACKEND.parent / "docker-compose.yml"

PROBE_APP = '''
async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})
'''


def _shipped_flags():
    """The uvicorn flags the image's CMD carries, minus the bind address."""
    cmd = None
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("CMD "):
            cmd = json.loads(line[4:])
    assert cmd and cmd[:2] == ["uvicorn", "app.main:app"], cmd
    flags, rest = [], iter(cmd[2:])
    for arg in rest:
        if arg in ("--host", "--port"):
            next(rest)
            continue
        flags.append(arg)
    return flags


@pytest.fixture(scope="module")
def shipped_server(tmp_path_factory):
    work = tmp_path_factory.mktemp("probe")
    (work / "probe_app.py").write_text(PROBE_APP, encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "probe_app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning", *_shipped_flags()],
        cwd=str(work), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        yield port
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _status(port, header_bytes, pieces):
    """Send one request whose Authorization header is header_bytes long, whole
    or in 2 KB pieces (a slow or hostile client), and return the status code -
    None when the server cut the connection instead of answering."""
    head = (b"GET / HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer " + b"a" * header_bytes
            + b"\r\nConnection: close\r\n\r\n")
    step = 2048 if pieces else len(head)
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        for i in range(0, len(head), step):
            s.sendall(head[i:i + step])
            if pieces:
                time.sleep(0.001)
        data = s.recv(64)
    except OSError:
        data = b""
    finally:
        s.close()
    parts = data.split(b" ", 2)
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


@pytest.mark.parametrize("pieces", [False, True], ids=["whole", "pieces"])
def test_a_head_past_the_bound_never_reaches_the_app(shipped_server, pieces):
    assert _status(shipped_server, 200_000, pieces) != 200


@pytest.mark.parametrize("pieces", [False, True], ids=["whole", "pieces"])
def test_a_large_legitimate_head_still_passes(shipped_server, pieces):
    # Above the ~32 KiB the shipped nginx forwards at most, so no request the
    # proxy lets through can meet the backend's bound.
    assert _status(shipped_server, 30_000, pieces) == 200


# --- the token is bounded before any decode ---------------------------------

def _padded_bearer():
    """A token this instance DID sign, padded past the bound: valid, so only
    the length check can refuse it."""
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    claims = {"sub": "1", "role": "admin", "exp": exp, "pad": "p" * auth.MAX_CREDENTIAL_CHARS}
    return jwt.encode(claims, auth.SECRET_KEY, algorithm=auth.ALGORITHM)


def test_the_middleware_refuses_an_over_long_bearer_before_decoding(monkeypatch):
    monkeypatch.setattr(auth, "ENABLE_AUTH", True)
    mini = FastAPI()
    mini.add_middleware(auth.AuthMiddleware)

    @mini.get("/api/secret")
    def secret():
        return {"ok": True}

    c = TestClient(mini)
    with patch.object(auth.jwt, "decode") as decode:
        r = c.get("/api/secret", headers={"Authorization": "Bearer " + _padded_bearer()})
    assert r.status_code == 401
    decode.assert_not_called()


def test_decode_access_token_refuses_an_over_long_token_before_decoding():
    import app.jwt_auth as jwt_auth
    with patch.object(jwt_auth.jwt, "decode") as decode:
        with pytest.raises(HTTPException) as refused:
            jwt_auth.decode_access_token(_padded_bearer())
    assert refused.value.status_code == 401
    decode.assert_not_called()


# --- the shipped compose: one door ------------------------------------------

@pytest.mark.skipif(not COMPOSE.exists(), reason="docker-compose.yml is not in this checkout")
def test_the_shipped_compose_publishes_the_backend_on_loopback_only():
    text = COMPOSE.read_text(encoding="utf-8")
    backend = text.split("\n  backend:", 1)[1].split("\n  frontend:", 1)[0]
    published = [ln.strip() for ln in backend.splitlines() if ln.strip().startswith('- "') and ":8000" in ln]
    assert published == ['- "127.0.0.1:8000:8000"'], published
