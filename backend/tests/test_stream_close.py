"""Stop stops the model (2026-10-01).

Starlette 0.41 iterates a sync body generator in a worker thread; when the
client disconnects mid-answer it cancels the response's task group and runs the
background task, but never closes the generator. A chat answer's generator
holds the provider's open HTTP stream, so after a Stop the model kept
generating - on a vendor model, billed output tokens - until the garbage
collector finalised it.

These tests disconnect a client for real at the ASGI layer (TestClient cannot
drop a socket), the way uvicorn 0.32 reports it (ASGI http spec 2.3: an
http.disconnect message), and read the stream's state the moment the response
returns, inside the event loop - after the loop shuts down, its async-generator
cleanup would close the generator too and hide the difference.

A server on ASGI http spec 2.4 reports the same disconnect differently: no
http.disconnect message, but an OSError from send(). Starlette lets that error
out of the response (1.x as ClientDisconnect) before it awaits the background
task - read on 0.41.3 and 1.10.0 alike, 2026-11-22 - so on that path the turn's
BackgroundTask release does not run, and the generator's close
(turn_guard.guarded_stream's finally) is the release. uvicorn 0.32 speaks 2.3;
the 2.4 tests below hold the guarantee for the day a server upgrade moves to 2.4.
"""
import asyncio
import json
from unittest.mock import patch

import pytest
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from app import turn_guard
from app.closing_stream import ClosingStreamingResponse

_SCOPE = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}
_LONG = 200


def _tracked(state, n=_LONG):
    """A body generator that records how far it got and whether it was closed."""
    try:
        for i in range(n):
            state["produced"] += 1
            yield f"chunk {i}\n"
    finally:
        state["closed"] = True


async def _drive(asgi, scope, *, request_body=None, leave_when=lambda chunk: True):
    """Run an ASGI callable as a client that sends `request_body` (if any) and
    goes away once a response body chunk satisfies `leave_when`."""
    gone = asyncio.Event()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if request_body is not None and not body_sent:
            body_sent = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if (message["type"] == "http.response.body" and message.get("body")
                and leave_when(message["body"])):
            gone.set()

    await asgi(scope, receive, send)


def _disconnect_and_read(response, state):
    async def main():
        await _drive(response, _SCOPE)
        return dict(state)  # read before the loop's shutdown can touch the generator
    return asyncio.run(main())


def test_starlette_alone_leaves_the_generator_open_after_a_disconnect():
    """Why ClosingStreamingResponse exists, pinned. If this starts failing,
    Starlette closes the generator itself now and the class can be retired."""
    state = {"produced": 0, "closed": False}
    seen = _disconnect_and_read(StreamingResponse(_tracked(state)), state)
    assert 0 < seen["produced"] < _LONG        # the client left early
    assert seen["closed"] is False             # ... and the generator was left open


def test_the_closing_response_closes_the_generator_after_a_disconnect():
    state = {"produced": 0, "closed": False}
    seen = _disconnect_and_read(ClosingStreamingResponse(_tracked(state)), state)
    assert 0 < seen["produced"] < _LONG
    assert seen["closed"] is True


_SCOPE_2_4 = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}


async def _drive_2_4(asgi, scope, *, request_body=None, leave_when=lambda chunk: True):
    """The same client against an ASGI http spec 2.4 server: it never sends
    http.disconnect; once it has gone, the server's send() raises OSError.
    Returns what the app let out (starlette 1.x: ClientDisconnect), or None."""
    gone = False
    body_sent = False

    async def receive():
        nonlocal body_sent
        if request_body is not None and not body_sent:
            body_sent = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        nonlocal gone
        if gone:
            raise OSError("the client went away")
        if (message["type"] == "http.response.body" and message.get("body")
                and leave_when(message["body"])):
            gone = True

    try:
        await asyncio.wait_for(asgi(scope, receive, send), timeout=30)
    except asyncio.TimeoutError:
        raise
    except Exception as e:  # noqa: BLE001 - the app's answer to a lost client is the point
        return e
    return None


def test_on_asgi_2_4_starlette_skips_the_background_task_after_a_disconnect():
    """Why the generator close has to carry the release on its own: on a 2.4
    server starlette never reaches the background task once send() has failed
    (0.41.3 and 1.10.0 alike). If this starts failing, starlette runs it on that
    path now, and the release has a second leg there too."""
    state, ran = {"produced": 0, "closed": False}, []
    response = StreamingResponse(_tracked(state), background=BackgroundTask(ran.append, 1))

    async def main():
        return await _drive_2_4(response, _SCOPE_2_4)

    let_out = asyncio.run(main())
    assert let_out is not None, "the lost client reached no exception"
    assert 0 < state["produced"] < _LONG
    assert ran == []


def test_the_closing_response_closes_the_generator_on_asgi_2_4_too():
    state = {"produced": 0, "closed": False}

    async def main():
        await _drive_2_4(ClosingStreamingResponse(_tracked(state)), _SCOPE_2_4)
        return dict(state)

    seen = asyncio.run(main())
    assert 0 < seen["produced"] < _LONG
    assert seen["closed"] is True


def test_a_stream_that_runs_to_the_end_is_whole_and_closing_it_again_is_harmless():
    state = {"produced": 0, "closed": False}
    body = []

    async def main():
        async def receive():
            await asyncio.Event().wait()   # the client never leaves

        async def send(message):
            if message["type"] == "http.response.body":
                body.append(message.get("body", b""))

        await ClosingStreamingResponse(_tracked(state, n=3))(_SCOPE, receive, send)
        return dict(state)

    seen = asyncio.run(main())
    assert b"".join(body) == b"chunk 0\nchunk 1\nchunk 2\n"
    assert seen == {"produced": 3, "closed": True}


def test_an_async_iterable_body_is_left_to_starlette():
    async def agen():
        yield b"x"

    assert ClosingStreamingResponse(agen())._source is None


# ---- the chat route ---------------------------------------------------------

def _guest_cfg(key, default=None):
    """The admin half of the guest gate on; everything else at its default."""
    return "true" if key == "guest_mode_enabled" else default


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_a_stop_in_the_chat_client_closes_the_provider_stream(client, spec):
    """The whole route, with a provider stream that records whether it was
    closed: the client leaves after the first token, and by the time the
    response returns the provider's stream is closed and the turn slot free."""
    from app.main import app

    provider = {"produced": 0, "closed": False}

    def stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        try:
            for i in range(_LONG):
                provider["produced"] += 1
                yield {"type": "text", "text": f"word{i} "}
        finally:
            provider["closed"] = True

    session = f"sc-stop-{spec}"
    body = json.dumps({"prompt": "Hi", "model": "test-model",
                       "session_id": session, "history": []}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec},
             "http_version": "1.1", "method": "POST", "path": "/api/chat",
             "raw_path": b"/api/chat", "root_path": "", "scheme": "http",
             "query_string": b"", "client": ("testclient", 50000),
             "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"),
                         (b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode())]}

    async def main():
        drive = _drive if spec == "2.3" else _drive_2_4
        await drive(app, scope, request_body=body,
                    leave_when=lambda chunk: b'"token"' in chunk)
        return dict(provider)

    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=stream):
        seen = asyncio.run(main())

    assert 0 < seen["produced"] < _LONG, "the client did not leave mid-answer"
    assert seen["closed"] is True, "the provider stream was left open after the client left"
    assert not turn_guard.in_flight("g:testclient", session)


def test_the_chat_route_answers_with_the_closing_response(client, monkeypatch):
    """The wiring, read through the route: the response it builds is the
    closing kind, with the turn's release still attached for the slot."""
    import app.routers.chat as chat_mod
    from starlette.background import BackgroundTask
    captured = {}
    real = chat_mod.ClosingStreamingResponse

    class Capturing(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["resp"] = self

    def plain(messages, model, tools=None, system_prompt="", max_tokens=1024):
        yield {"type": "text", "text": "ok"}

    monkeypatch.setattr(chat_mod, "ClosingStreamingResponse", Capturing)
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=plain):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model",
                                           "session_id": "sc-wiring", "history": []})
    assert r.status_code == 200 and "ok" in r.text
    resp = captured["resp"]
    assert resp._source is not None
    assert isinstance(resp.background, BackgroundTask)
    assert resp.background.func is turn_guard.release
