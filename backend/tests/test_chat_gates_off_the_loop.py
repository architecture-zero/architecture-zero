"""The chat route's gates run OFF the event loop (2026-11-20).

THE DEFECT. chat() is async and called the synchronous _chat_gates directly,
so the gates ran ON the event loop and every other request on the process
waited for them: the rate limit's window read, the config reads, and
model_catalog.resolve - reached through _provider_for_model when a caller
names a model, and again at the gates' end - whose first family alias after a
restart (no model list cached yet) reads Anthropic's model list inline, a
blocking HTTP call of up to 5 s.

THE FIX. chat() runs _chat_gates on a worker thread
(anyio.to_thread.run_sync). A refusal raised there is raised in chat()
unchanged, and request.model is still resolved in place before the answer.

FIX tests fail on the tree before: the held step runs on the loop, so
/api/health cannot start until it returns - bounded by HOLD, never a hang.
CONTROL tests hold on both trees.
"""
import asyncio
import threading
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app import model_catalog
from app.main import app
from app.routers import chat as chat_mod

HOLD = 3.0


class _Held:
    """A step standing still: blocks its caller's thread until released or
    HOLD passes, then returns `answer`. Counts the callers inside at once."""

    def __init__(self, answer=None, wait_for=1):
        self.answer = answer
        self.wait_for = wait_for      # release only once this many are inside
        self._lock = threading.Lock()
        self.inside = 0
        self.peak = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()
        self.released_in_time = None

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
            if self.inside >= self.wait_for:
                self.entered.set()
        try:
            self.released_in_time = self.release.wait(HOLD)
        finally:
            with self._lock:
                self.inside -= 1
        self.returned.set()
        return self.answer


def _held_request(client, method, path, stub, **kwargs):
    """Send one request whose slow step is `stub` and ask /api/health while it
    is held. Returns (entered, health, still_held, response)."""

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     timeout=HOLD * 4) as c:
            call = asyncio.create_task(c.request(method, path, **kwargs))
            try:
                deadline = time.monotonic() + HOLD * 2
                while not stub.entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                entered = stub.entered.is_set()
                health = await c.get("/api/health")
                still_held = not stub.returned.is_set()
            finally:
                stub.release.set()
            r = await asyncio.wait_for(call, HOLD * 4)
            return entered, health, still_held, r

    return client.portal.call(go)


def _assert_not_blocking(entered, health, still_held, stub):
    assert entered, "the request never reached its held step"
    assert health.status_code == 200, health.text[:200]
    assert still_held, (
        "/api/health was answered only after the held step returned - it ran on "
        "the event loop and held every other request behind it")
    assert stub.released_in_time is True, "the held step waited out its whole hold"


@pytest.fixture
def answer_stub(monkeypatch):
    """The answer after the gates, replaced: records the model the gates left
    on the request and answers at once. The gates are what these tests read.
    chat() took the turn guard's slot before calling it, and a real answer
    frees it when its stream ends; this one frees it here."""
    seen = {}

    async def fake_answer(request, req, current_user, _t0, caller, token):
        from app import turn_guard
        turn_guard.release(caller, request.session_id, token)
        seen["model"] = request.model
        return JSONResponse({"answered": True})

    monkeypatch.setattr(chat_mod, "_chat_answer", fake_answer)
    return seen


# -- FIX: the chat gates ------------------------------------------------------

def test_a_held_gate_does_not_stall_other_requests(client, admin_headers, monkeypatch,
                                                   answer_stub):
    stub = _Held(None)
    monkeypatch.setattr(chat_mod, "check_rate_limit", stub)
    seen = _held_request(client, "POST", "/api/chat", stub, headers=admin_headers,
                         json={"prompt": "hello", "session_id": "loop-gate"})
    _assert_not_blocking(*seen[:3], stub)
    assert seen[3].status_code == 200, seen[3].text[:200]
    assert seen[3].json() == {"answered": True}


def test_the_first_alias_lookup_after_a_restart_does_not_stall_other_requests(
        client, admin_headers, monkeypatch, answer_stub):
    """The exact defect: no model list yet (a fresh process), so the first
    family alias a caller names reads Anthropic's list inline - here inside
    the gates' _provider_for_model, held. The provider gate is told Anthropic
    is offered so the alias's turn gets past it to the answer."""
    stub = _Held([])
    monkeypatch.setattr(model_catalog, "_fetch_list", stub)
    monkeypatch.setattr(model_catalog, "_list_cache", {"ts": 0.0, "ttl": 0.0, "models": None})
    monkeypatch.setattr(chat_mod, "offered_providers", lambda: {"anthropic", "ollama"})
    seen = _held_request(client, "POST", "/api/chat", stub, headers=admin_headers,
                         json={"prompt": "hello", "session_id": "loop-alias",
                               "model": "claude-opus-latest"})
    _assert_not_blocking(*seen[:3], stub)
    assert seen[3].status_code == 200, seen[3].text[:200]
    # The list could not be read, so the alias took its fallback - resolved.
    assert answer_stub["model"] != "claude-opus-latest"


# -- CONTROL: what the gates decide is unchanged ------------------------------

def test_a_gate_refusal_still_refuses(client, admin_headers, monkeypatch, answer_stub):
    def limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests")
    monkeypatch.setattr(chat_mod, "check_rate_limit", limited)
    r = client.post("/api/chat", headers=admin_headers,
                    json={"prompt": "hello", "session_id": "loop-refuse"})
    assert r.status_code == 429
    assert "model" not in answer_stub, "a refused turn reached the answer"


def test_the_model_the_gates_resolve_reaches_the_answer(client, admin_headers, monkeypatch,
                                                         answer_stub):
    monkeypatch.setattr(model_catalog, "resolve", lambda m: f"resolved:{m}")
    r = client.post("/api/chat", headers=admin_headers,
                    json={"prompt": "hello", "session_id": "loop-resolve"})
    assert r.status_code == 200, r.text[:200]
    assert answer_stub["model"].startswith("resolved:")
