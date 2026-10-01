"""One turn at a time per caller and session on /api/chat (2026-09-28).

A load test on a fork had shown two correctness defects on a shared session:
two concurrent turns persisted `u u a a`, and a duplicate submission answered
twice. The endpoint tests below RACE two turns for real - the mocked model
stream announces it started and then blocks on an Event until the test lets it
go - which no sequential test can show.
"""
import threading
from unittest.mock import patch

import pytest

from app import turn_guard
from app.history import load_history


@pytest.fixture(autouse=True)
def _clean_registry():
    turn_guard._reset_for_tests()
    yield
    turn_guard._reset_for_tests()


# ---- the registry -----------------------------------------------------------

def test_second_acquire_on_the_same_key_is_refused_until_released():
    token = turn_guard.acquire("u:1", "s")
    assert token
    assert turn_guard.acquire("u:1", "s") is None
    assert turn_guard.release("u:1", "s", token) is True
    assert turn_guard.acquire("u:1", "s")


def test_other_sessions_and_other_callers_are_independent():
    assert turn_guard.acquire("u:1", "s1")
    assert turn_guard.acquire("u:1", "s2")
    assert turn_guard.acquire("u:2", "s1")
    # Two guests on the shared default session id never block each other.
    assert turn_guard.acquire("g:10.0.0.9", "default")
    assert turn_guard.acquire("g:10.0.0.8", "default")


def test_release_with_the_wrong_token_is_a_noop():
    token = turn_guard.acquire("u:1", "s")
    assert turn_guard.release("u:1", "s", "not-the-token") is False
    assert turn_guard.acquire("u:1", "s") is None
    assert turn_guard.release("u:1", "s", token) is True


def test_a_stale_entry_is_taken_over_and_its_old_token_cannot_free_the_new_holder(monkeypatch):
    monkeypatch.setenv("CHAT_TURN_TTL_SECONDS", "100")
    old = turn_guard.acquire("u:1", "s", now=1000.0)
    assert old
    assert turn_guard.acquire("u:1", "s", now=1050.0) is None      # still fresh: refused
    new = turn_guard.acquire("u:1", "s", now=1100.5)               # past the TTL: taken over
    assert new and new != old
    assert turn_guard.release("u:1", "s", old) is False            # the stale turn's release is ignored
    assert turn_guard.in_flight("u:1", "s", now=1101.0)            # the new holder still holds
    assert turn_guard.release("u:1", "s", new) is True
    assert not turn_guard.in_flight("u:1", "s", now=1101.0)


def test_a_stale_entry_is_swept_on_any_acquire(monkeypatch):
    monkeypatch.setenv("CHAT_TURN_TTL_SECONDS", "100")
    assert turn_guard.acquire("u:1", "abandoned", now=1000.0)
    assert turn_guard.acquire("u:2", "other", now=1050.0)
    assert turn_guard.acquire("u:3", "third", now=1101.0)
    assert not turn_guard.in_flight("u:1", "abandoned", now=1101.0)
    assert turn_guard.in_flight("u:2", "other", now=1101.0)


def test_the_ttl_env_falls_back_on_a_bad_value(monkeypatch):
    monkeypatch.setenv("CHAT_TURN_TTL_SECONDS", "soon")
    assert turn_guard.ttl_seconds() == 600
    monkeypatch.setenv("CHAT_TURN_TTL_SECONDS", "-5")
    assert turn_guard.ttl_seconds() == 600
    monkeypatch.setenv("CHAT_TURN_TTL_SECONDS", "45")
    assert turn_guard.ttl_seconds() == 45


def test_caller_key_shape():
    assert turn_guard.caller_key(7, "1.2.3.4") == "u:7"
    assert turn_guard.caller_key(None, "1.2.3.4") == "g:1.2.3.4"


def _items(items, fail_at=None):
    for i, item in enumerate(items):
        if fail_at is not None and i == fail_at:
            raise RuntimeError("boom")
        yield item


def test_guarded_stream_frees_the_slot_on_exhaustion_on_error_and_on_close():
    token = turn_guard.acquire("u:1", "s")
    assert list(turn_guard.guarded_stream(_items(["a", "b"]), "u:1", "s", token)) == ["a", "b"]
    assert not turn_guard.in_flight("u:1", "s")

    token = turn_guard.acquire("u:1", "s")
    with pytest.raises(RuntimeError):
        list(turn_guard.guarded_stream(_items(["a", "b"], fail_at=1), "u:1", "s", token))
    assert not turn_guard.in_flight("u:1", "s")

    token = turn_guard.acquire("u:1", "s")
    gen = turn_guard.guarded_stream(_items(["a", "b"]), "u:1", "s", token)
    assert next(gen) == "a"
    assert turn_guard.in_flight("u:1", "s")
    gen.close()
    assert not turn_guard.in_flight("u:1", "s")


# ---- the endpoint, raced for real -------------------------------------------

def _guest_cfg(key, default=None):
    """The admin half of the guest gate on; everything else at its default."""
    return "true" if key == "guest_mode_enabled" else default


class _BlockingStream:
    """A model stream whose FIRST call announces it started and then waits to be
    released, so a second request can be fired while the first is provably
    mid-answer; later calls stream at once."""

    def __init__(self):
        self.started = threading.Event()
        self.go = threading.Event()
        self.calls = 0

    def __call__(self, messages, model, tools=None, system_prompt="", max_tokens=1024):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            assert self.go.wait(timeout=20), "the test never released the stream"
        yield {"type": "text", "text": "Hello"}


def _post(client, session, prompt="Hi", headers=None):
    return client.post("/api/chat", json={"prompt": prompt, "model": "test-model",
                                          "session_id": session, "history": []},
                       headers=headers or {})


def _roles(client, session, headers):
    r = client.get(f"/api/history/{session}", headers=headers)
    assert r.status_code == 200
    return [m["role"] for m in r.json()["messages"]]


def _patched(stream):
    return (patch("app.routers.chat.guest_chat_available", return_value=True),
            patch("app.routers.chat.get_config", side_effect=_guest_cfg),
            patch("app.routers.chat.stream_chat_events", side_effect=stream))


def test_a_second_turn_on_a_busy_session_is_refused_and_writes_nothing(client, admin_headers):
    blocking = _BlockingStream()
    busy, other = "tg-busy", "tg-other"
    got = {}
    p1, p2, p3 = _patched(blocking)
    with p1, p2, p3:
        worker = threading.Thread(target=lambda: got.update(a=_post(client, busy, "first", admin_headers)))
        worker.start()
        assert blocking.started.wait(timeout=20), "the first turn never reached the model"
        try:
            # Mid-answer: the same caller's second turn on the same session is
            # refused at the door - no retrieval, no model, no row.
            b = _post(client, busy, "second", admin_headers)
            assert b.status_code == 409
            assert b.json()["detail"] == turn_guard.IN_FLIGHT_DETAIL
            # The same caller's turn on ANOTHER session is not blocked.
            c = _post(client, other, "elsewhere", admin_headers)
            assert c.status_code == 200
        finally:
            blocking.go.set()
            worker.join(timeout=30)
    assert got["a"].status_code == 200
    assert "Hello" in got["a"].text
    # Two model calls in all - the first turn and the other session's; the
    # refused turn never reached the model.
    assert blocking.calls == 2
    # The transcript stays paired: the refused turn left no user row behind.
    assert _roles(client, busy, admin_headers) == ["user", "assistant"]
    assert _roles(client, other, admin_headers) == ["user", "assistant"]


def _plain_stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "ok"}


def test_sequential_turns_on_one_session_pass_and_persist_paired(client):
    session = "tg-sequential"
    p1, p2, p3 = _patched(_plain_stream)
    with p1, p2, p3:
        assert _post(client, session, "one").status_code == 200
        assert _post(client, session, "two").status_code == 200
    # Guests key by client address; the history read is scoped the same way.
    assert [m["role"] for m in load_history(session, None)] == ["user", "assistant", "user", "assistant"]


def _failing_stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "partial"}
    raise RuntimeError("provider fell over")


def test_a_stream_that_fails_frees_the_slot_for_the_next_turn(client):
    session = "tg-stream-error"
    p1, p2, p3 = _patched(_failing_stream)
    with p1, p2, p3:
        r = _post(client, session, "one")
        assert r.status_code == 200          # the failure crosses the wire as an SSE error frame
        assert '"error"' in r.text
    p1, p2, p3 = _patched(_plain_stream)
    with p1, p2, p3:
        assert _post(client, session, "two").status_code == 200


def test_the_response_carries_a_token_checked_release_for_the_disconnect_path(client, monkeypatch):
    """Starlette 0.41 does not close a sync generator when the client
    disconnects mid-stream (the task group is cancelled, the generator is
    left to the garbage collector). Since 2026-10-01 the route's
    ClosingStreamingResponse closes it, which runs guarded_stream's finally
    (tests/test_stream_close.py drops a real connection); the route also still
    hands the response a BackgroundTask that releases with the turn's own
    token, which Starlette awaits after the task group exits on the normal AND
    the disconnect path - a second release, a no-op when the first ran. This
    pins that wiring and that the task, taken alone on a held slot, frees it."""
    import anyio
    from starlette.background import BackgroundTask
    import app.routers.chat as chat_mod
    captured = {}
    real = chat_mod.ClosingStreamingResponse

    class Capturing(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["resp"] = self

    monkeypatch.setattr(chat_mod, "ClosingStreamingResponse", Capturing)
    session = "tg-background"
    p1, p2, p3 = _patched(_plain_stream)
    with p1, p2, p3:
        assert _post(client, session, "one").status_code == 200
    bg = captured["resp"].background
    assert isinstance(bg, BackgroundTask) and bg.func is turn_guard.release
    caller, sess, token = bg.args
    assert (caller, sess) == ("g:testclient", session)
    assert not turn_guard.in_flight(caller, sess)
    assert turn_guard.release(caller, sess, token) is False
    held = turn_guard.acquire(caller, sess)
    assert held and turn_guard.in_flight(caller, sess)
    anyio.run(BackgroundTask(turn_guard.release, caller, sess, held))
    assert not turn_guard.in_flight(caller, sess)


def test_a_raise_before_the_response_frees_the_slot(client):
    session = "tg-pre-stream"
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_cfg), \
         patch("app.rerank.retrieve", side_effect=RuntimeError("index exploded")):
        with pytest.raises(RuntimeError):
            client.post("/api/chat", json={"prompt": "one", "model": "test-model",
                                           "session_id": session, "history": [],
                                           "use_rag": True})
    assert not turn_guard.in_flight("g:testclient", session)
    p1, p2, p3 = _patched(_plain_stream)
    with p1, p2, p3:
        assert _post(client, session, "two").status_code == 200
