"""The request-body ceiling BEFORE the JSON parser (app/body_limit.py).

The chat route's character bound runs inside the handler, after FastAPI has
read and parsed the whole body, so it bounds what reaches the provider and
nothing else. The routes a caller reaches before signing in (chat, login,
refresh, setup, MFA completion) would otherwise hand a 60 MB body to
json.loads - and wherever a deployment publishes the backend's own port (the
shipped compose did on every interface until 2026-10-01; it binds loopback now)
the proxy's own ceiling covers only one door. These pin the ceiling at
the app, the document doors' own figures, and the chunked path.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.body_limit import BodySizeLimit
from app.routers.kb import MAX_UPLOAD_MB
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


def test_the_ceiling_is_wired_with_its_document_doors():
    m = _installed()
    assert m.kwargs["default_limit"] == MAX_JSON_BODY_BYTES
    per_path = m.kwargs["per_path"]
    # Exactly these two, so a third door has to be added on purpose.
    assert set(per_path) == {"/api/ingest", "/api/ingest/upload"}
    assert per_path["/api/ingest"] == MAX_UPLOAD_MB * 1024 * 1024      # the JSON text-ingest door
    assert per_path["/api/ingest/upload"] == (MAX_UPLOAD_MB + 1) * 1024 * 1024
    inst = BodySizeLimit(lambda *a: None, default_limit=m.kwargs["default_limit"], per_path=per_path)
    assert inst.limit_for("/api/chat") == MAX_JSON_BODY_BYTES
    assert inst.limit_for("/api/auth/login") == MAX_JSON_BODY_BYTES
    assert inst.limit_for("/api/ingest/upload") == (MAX_UPLOAD_MB + 1) * 1024 * 1024


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


def test_a_chunked_oversize_body_is_a_json_413_through_the_whole_stack(client):
    """The same refusal end to end, with no Content-Length to read: the count
    trips INSIDE FastAPI's body read, under every middleware the app carries,
    and the caller must still get the JSON 413 - not a 500 from an exception
    nobody handled, and not the 400 the parse wrapper turns a stray exception
    into. The unit test above proves the counting; this proves the stack
    around it lets the refusal out."""
    def body():
        yield b'{"prompt": "'
        yield b"x" * (MAX_JSON_BODY_BYTES + 16)
        yield b'"}'

    with patch("app.routers.chat.check_injection") as scan:
        r = client.post("/api/chat", content=body(),
                        headers={"Content-Type": "application/json"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413
    assert "Request body too large" in r.json()["detail"]
    scan.assert_not_called()

def test_zero_disables_the_ceiling():
    """0 switches the default off - and the test has to see the body GO
    THROUGH. As first written it asserted nothing, so it passed just as well
    against a ceiling that refused."""
    pulled = []
    sent = []

    async def inner(scope, receive, send):
        message = await receive()
        pulled.append(len(message["body"]))

    mw = BodySizeLimit(inner, default_limit=0, per_path={})

    async def receive():
        return {"type": "http.request", "body": b"x" * 10_000, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(mw({"type": "http", "path": "/api/chat",
                    "headers": [(b"content-length", b"10000")]}, receive, send))
    assert pulled == [10_000]
    assert sent == []


# -- what the reviews asked for (2026-09-28): the upload door, the credential, zero, the record --

_MB = 1024 * 1024


def _drive(path, headers, pieces):
    """Call the app's ASGI interface directly with a body that arrives in
    pieces, and count what the app PULLED before it answered. The test client
    hands a whole body over as one message, so it cannot show where intake
    stops; this can."""
    from app.main import app
    gen = iter(pieces)
    seen = {"pulled": 0, "at_answer": None, "status": None, "body": b""}

    async def receive():
        try:
            piece = next(gen)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        seen["pulled"] += len(piece)
        return {"type": "http.request", "body": piece, "more_body": True}

    async def send(message):
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]
            seen["at_answer"] = seen["pulled"]
        elif message["type"] == "http.response.body":
            seen["body"] += message.get("body", b"")

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "path": path, "raw_path": path.encode(), "root_path": "",
             "scheme": "http", "query_string": b"", "client": ("127.0.0.1", 50000),
             "server": ("testserver", 80),
             "headers": [(b"host", b"testserver")] + [
                 (k.lower().encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(app(scope, receive, send))
    return seen


def _through_the_installed_ceiling(path, headers, pieces):
    """The INSTALLED middleware alone, in front of a stand-in that reads the
    whole body - so the answer depends on the wiring and on nothing else."""
    m = _installed()
    seen = {"reached": None, "sent": []}
    gen = iter(pieces)

    async def inner(scope, receive, send):
        total = 0
        while True:
            message = await receive()
            total += len(message.get("body") or b"")
            if not message.get("more_body"):
                break
        seen["reached"] = total

    async def receive():
        piece = next(gen, None)
        if piece is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": piece, "more_body": True}

    async def send(message):
        seen["sent"].append(message)

    mw = BodySizeLimit(inner, **m.kwargs)
    try:
        asyncio.run(mw({"type": "http", "path": path,
                        "headers": [(k.lower().encode(), v.encode())
                                    for k, v in headers.items()]}, receive, send))
    except HTTPException as refused:
        seen["raised"] = refused.status_code
    return seen


def test_a_declared_oversize_body_is_refused_before_a_byte_is_read():
    """The docstring's own claim, pinned. Without this the early check could be
    deleted and every other test here stay green: the streaming count refuses
    the same body in the same words, only after reading it."""
    pulled = []

    async def inner(scope, receive, send):
        pulled.append("inner ran")

    async def receive():
        pulled.append("a byte was read")
        return {"type": "http.request", "body": b"x", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    mw = BodySizeLimit(inner, default_limit=100, per_path={})
    asyncio.run(mw({"type": "http", "path": "/api/chat",
                    "headers": [(b"content-length", b"101")]}, receive, send))
    assert pulled == []
    assert sent[0]["status"] == 413
    assert b"Request body too large" in sent[1]["body"]


def test_the_upload_door_carries_a_ceiling():
    """It was exempt (None) until 2026-09-28. The handler stops reading the
    FILE at its limit; nothing stopped the app TAKING IN the body, because the
    multipart parser runs before the handler and spools without a limit."""
    m = _installed()
    ceiling = m.kwargs["per_path"]["/api/ingest/upload"]
    assert ceiling == (MAX_UPLOAD_MB + 1) * _MB      # the file plus its envelope
    assert None not in m.kwargs["per_path"].values()  # nothing shipped is exempt


def test_the_upload_door_refuses_a_declared_oversize_body_before_its_parser(admin_headers):
    ceiling = _installed().kwargs["per_path"]["/api/ingest/upload"]

    def never():
        raise AssertionError("the body was read")
        yield b""                                     # pragma: no cover

    seen = _drive("/api/ingest/upload",
                  {**admin_headers, "Content-Type": "multipart/form-data; boundary=b",
                   "Content-Length": str(ceiling + 1)}, never())
    assert seen["status"] == 413 and seen["pulled"] == 0
    assert b"Request body too large" in seen["body"]


def test_the_upload_door_stops_taking_in_at_its_ceiling(admin_headers):
    """No Content-Length, a file part that never ends: intake stops within one
    piece of the ceiling. Before the ceiling this took the whole body in (80 MB
    measured) and answered 413 from the handler afterwards."""
    ceiling = _installed().kwargs["per_path"]["/api/ingest/upload"]
    piece = b"a" * (4 * _MB)

    def body():
        yield (b"--b\r\n"
               b'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
               b"Content-Type: text/plain\r\n\r\n")
        for _ in range((ceiling // len(piece)) + 8):
            yield piece

    seen = _drive("/api/ingest/upload",
                  {**admin_headers, "Content-Type": "multipart/form-data; boundary=b",
                   "Transfer-Encoding": "chunked"}, body())
    assert seen["status"] == 413
    assert b"Request body too large" in seen["body"]
    assert seen["at_answer"] <= ceiling + len(piece) + 1024


def test_the_upload_handler_still_bounds_the_file_itself(client, admin_headers):
    """The ceiling is the file's figure PLUS room for its envelope, so a file
    just over the limit passes the ceiling and meets the handler's own cap.
    A test that sends more than that is answered by the ceiling, and would
    stay green with the handler's cap deleted."""
    body = b"x" * (MAX_UPLOAD_MB * _MB + 512 * 1024)
    r = client.post("/api/ingest/upload",
                    files={"file": ("over.txt", body, "text/plain")},
                    data={"department": "general"}, headers=admin_headers)
    assert r.status_code == 413, r.text[:200]
    assert "File too large" in r.json()["detail"]


def test_a_refused_body_is_counted_and_logged_with_figures_only():
    async def inner(scope, receive, send):
        await receive()

    async def receive():
        return {"type": "http.request", "body": b"SECRET" * 100, "more_body": False}

    async def send(message):
        pass

    long_path = "/api/" + "p" * 500
    with patch("app.body_limit.increment") as count, patch("app.body_limit.log") as record:
        mw = BodySizeLimit(inner, default_limit=100, per_path={})
        record.reset_mock()                         # the startup line is pinned elsewhere
        with pytest.raises(HTTPException):
            asyncio.run(mw({"type": "http", "path": "/api/chat", "headers": []}, receive, send))
        asyncio.run(mw({"type": "http", "path": long_path,
                        "headers": [(b"content-length", b"101")]}, receive, send))
    assert [c.args[0] for c in count.call_args_list] == ["request_body_refused_total"] * 2
    assert [c.args[0] for c in record.call_args_list] == ["request_body_refused"] * 2
    streamed, announced = (c.kwargs for c in record.call_args_list)
    assert streamed == {"path": "/api/chat", "size": 600, "limit": 100, "declared": False}
    # the path is the caller's to choose, so it is cut before it is written
    assert announced == {"path": long_path[:200], "size": 101, "limit": 100, "declared": True}
    assert "SECRET" not in repr(record.call_args_list)


def test_a_wider_ceiling_is_only_for_a_caller_with_a_credential():
    """On an instance that runs with auth off the middleware outside admits
    everyone, and a document door's own guard answers only after its body has
    been read and parsed - so the wider figure was anyone's."""
    asked = []

    def admits(authorization):
        asked.append(authorization)
        return authorization == "Bearer good"

    inst = BodySizeLimit(lambda *a: None, default_limit=100,
                         per_path={"/doc": 5000, "/open": None, "/small": 10},
                         wider_for=admits)
    assert inst.limit_for("/doc") == 100                     # nothing presented
    assert inst.limit_for("/doc", "Bearer bad") == 100
    assert inst.limit_for("/doc", "Bearer good") == 5000
    assert inst.limit_for("/open", "") == 100                # "no ceiling" is the widest figure there is
    assert inst.limit_for("/open", "Bearer good") is None
    asked.clear()
    assert inst.limit_for("/small", "") == 10                # a TIGHTER figure needs no credential
    assert inst.limit_for("/other", "") == 100
    assert asked == []                                       # and the check is not even asked


def test_a_check_that_raises_grants_nothing():
    def broken(authorization):
        raise RuntimeError("the check itself failed")

    inst = BodySizeLimit(lambda *a: None, default_limit=100, per_path={"/doc": 5000},
                         wider_for=broken)
    assert inst.limit_for("/doc", "Bearer anything") == 100


def test_the_installed_ceiling_asks_for_a_credential(admin_headers):
    from app.auth import presents_a_credential
    m = _installed()
    assert m.kwargs["wider_for"] is presents_a_credential
    inst = BodySizeLimit(lambda *a: None, **m.kwargs)
    assert m.kwargs["per_path"], "no door to check"
    for path, figure in m.kwargs["per_path"].items():
        assert figure > MAX_JSON_BODY_BYTES, path
        assert inst.limit_for(path, "") == MAX_JSON_BODY_BYTES, path
        assert inst.limit_for(path, "Bearer not-a-real-token") == MAX_JSON_BODY_BYTES, path
        assert inst.limit_for(path, admin_headers["Authorization"]) == figure, path


@pytest.mark.parametrize("presented", [
    {}, {"Authorization": "Bearer not-a-real-token"}, {"Authorization": "Basic abc"}])
def test_a_document_door_answers_a_caller_with_no_credential_on_the_headers(presented):
    """Through the installed middleware alone, so the answer does not depend
    on ENABLE_AUTH: this is what an instance running with auth off relies on.
    401, and NOTHING read - not even a body small enough for the default
    ceiling. Holding this caller to the default let them in: behind a proxy
    that streams the doors, a stalled upload was a request held in the app."""
    import json
    for path in _installed().kwargs["per_path"]:
        for headers, pieces in (({"Content-Length": "10"}, [b"x" * 10]),
                                ({"Content-Length": str(60 * _MB)}, [b"x" * _MB] * 60),
                                ({}, [b"x" * _MB] * 3)):
            with patch("app.body_limit.increment") as count, \
                 patch("app.body_limit.log") as record:
                seen = _through_the_installed_ceiling(path, {**presented, **headers}, pieces)
            assert seen["reached"] is None and "raised" not in seen, path   # never read
            assert seen["sent"][0]["status"] == 401, path
            assert json.loads(seen["sent"][1]["body"]) == {"detail": "Not authenticated"}
            assert [c.args[0] for c in count.call_args_list] == ["document_door_refused_total"]
            refused = [c for c in record.call_args_list if c.args[0] == "document_door_refused"]
            assert [c.kwargs for c in refused] == [{"path": path}]


def test_a_path_with_no_figure_of_its_own_asks_nobody_who_they_are():
    """The credential is for the doors. Everywhere else the ceiling is a
    ceiling: a caller with nothing to present is read up to the default."""
    seen = _through_the_installed_ceiling("/api/chat", {"Content-Length": "10"}, [b"x" * 10])
    assert seen["reached"] == 10 and seen["sent"] == []


def test_a_credentialed_body_over_the_default_reaches_a_document_door(admin_headers):
    """The other half, and the one that would ship green if the middleware
    stopped handing the header to the check: every real upload refused."""
    for path in _installed().kwargs["per_path"]:
        seen = _through_the_installed_ceiling(
            path, {**admin_headers, "Content-Length": str(3 * _MB)}, [b"x" * _MB] * 3)
        assert seen["reached"] == 3 * _MB, path
        assert seen["sent"] == [] and "raised" not in seen, path


def test_what_counts_as_a_credential(admin_headers):
    import time
    from jose import jwt
    from app import auth
    assert auth.presents_a_credential("") is False
    assert auth.presents_a_credential("Basic abc") is False
    assert auth.presents_a_credential("bearer " + admin_headers["Authorization"][7:]) is False
    assert auth.presents_a_credential("Bearer ") is False
    assert auth.presents_a_credential("Bearer not-a-real-token") is False
    assert auth.presents_a_credential(admin_headers["Authorization"]) is True

    def signed(claims, key=None):
        return "Bearer " + jwt.encode(claims, key or auth.SECRET_KEY, algorithm=auth.ALGORITHM)

    soon = int(time.time()) + 600
    assert auth.presents_a_credential(signed({"sub": "1", "exp": soon})) is True
    assert auth.presents_a_credential(signed({"sub": "1", "exp": int(time.time()) - 60})) is False
    assert auth.presents_a_credential(signed({"sub": "1", "exp": soon}, "another-signing-key")) is False
    assert auth.presents_a_credential(signed({"sub": "1"})) is False          # no expiry
    # a TYPED token is a step on the way to a session, not a session
    for kind in ("mfa", "sso", "refresh"):
        assert auth.presents_a_credential(signed({"sub": "1", "type": kind, "exp": soon})) is False, kind
    # nothing is decoded that is longer than a token this instance signs
    padded = signed({"sub": "1", "exp": soon, "pad": "p" * auth.MAX_CREDENTIAL_CHARS})
    with patch.object(auth.jwt, "decode") as decode:
        assert auth.presents_a_credential(padded) is False
    decode.assert_not_called()

    with patch.object(auth, "WATCHER_API_KEY", "watcher-key-for-this-test"):
        assert auth.presents_a_credential("Bearer watcher-key-for-this-test") is True
        assert auth.presents_a_credential("Bearer watcher-key-for-this-tesT") is False
        assert auth.presents_a_credential("Bearer watcher-key-for-this-tes") is False    # a prefix of it
        assert auth.presents_a_credential("Bearer watcher-key-for-this-test-") is False  # it, and more
    with patch.object(auth, "WATCHER_API_KEY", ""):
        assert auth.presents_a_credential("Bearer ") is False   # an unset key admits nobody


def test_a_default_switched_off_does_not_hand_a_door_to_everyone():
    """With the default at 0 a caller with NO credential used to get no
    ceiling at all on a door, while one WITH a credential was held to the
    door's figure. Nothing is wider than no ceiling: the door's own figure
    holds for every caller."""
    inst = BodySizeLimit(lambda *a: None, default_limit=0, per_path={"/doc": 5000},
                         wider_for=lambda authorization: False)
    assert inst.limit_for("/doc", "") == 5000
    assert inst.limit_for("/doc", "Bearer anything") == 5000
    assert inst.limit_for("/other", "") == 0


def test_a_figure_of_zero_or_less_is_a_mistake_not_an_exemption():
    """An upload size of 0 MB must not read as "no ceiling on the upload
    door". None is the one way to ask for that."""
    with patch("app.body_limit.log") as record:
        inst = BodySizeLimit(lambda *a: None, default_limit=100,
                             per_path={"/zero": 0, "/negative": -5, "/open": None,
                                       "/doc": 5000})
    assert set(inst.per_path) == {"/open", "/doc"}
    assert inst.limit_for("/zero") == 100 and inst.limit_for("/negative") == 100
    said = [c for c in record.call_args_list if c.args[0] == "request_body_ceiling"]
    assert len(said) == 1
    assert said[0].kwargs == {"default": 100, "doors": {"/open": "none", "/doc": 5000},
                              "ignored": {"/zero": 0, "/negative": -5},
                              "credential_check": False}


def test_the_installed_ceiling_has_no_figure_it_ignored():
    m = _installed()
    inst = BodySizeLimit(lambda *a: None, **m.kwargs)
    assert set(inst.per_path) == set(m.kwargs["per_path"])
