"""The self-check's Ollama probe needs Ollama's own answer, and reads the
saved toggle (2026-10-30).

probe_ollama read ok on any HTTP 200 and never read the body, so a wrong
base URL that answers HTML (a proxy's page, another service) read up on the
timer, the health routes and the Monitoring tab, and the ollama_down alert
fired only when the request raised. /api/status and the Settings Test
button had been cured of the same read that day, each with its own copy of
the shape check. Now ONE helper (self_check.ollama_model_list) decides for
all three: a 2xx whose body is an object carrying a "models" list.

And whether the timer watches Ollama at all was read from the ENABLE_OLLAMA
environment variable, while the chat, the picker and the Settings tab read
the toggle saved in Settings (providers.get_provider_config). An operator
who turned Ollama off in Settings still got ollama_down alerts; one who
turned it on got no watch. The pass now reads the saved toggle, every pass,
and the health routes' word follows what the last pass decided.

FIX: an HTML 200, a 200 without a model list and a 404 each read down AND
fire ollama_down; the saved toggle decides over the environment both ways;
the timer's wiring word follows the pass. CONTROL: a real model list (an
empty one included) reads up with no alert.
"""
import asyncio
import inspect

import pytest

from app import self_check
from app.config import get_config, set_config


class _Answer:
    """What the probe's reader returns, as far as the probe reads it."""

    def __init__(self, status_code: int, body=None, html: bool = False):
        self.status_code = status_code
        self._body = body
        self._html = html

    def json(self):
        if self._html:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


@pytest.fixture
def fired(monkeypatch):
    calls = []
    monkeypatch.setattr(self_check, "fire_alert",
                        lambda key, title, body: calls.append(key))
    monkeypatch.setattr(self_check, "_ollama_last", self_check._ollama_last)
    monkeypatch.setattr(self_check, "_ollama_wired", self_check._ollama_wired)
    monkeypatch.setattr(self_check, "_last_failing", ())
    return calls


@pytest.mark.parametrize("answer", [
    _Answer(200, html=True),
    _Answer(200, {"status": "ok", "service": "something else"}),
    _Answer(200, ["not", "an", "object"]),
    _Answer(404, {"models": []}),
], ids=["200-html", "200-without-a-model-list", "200-bare-list", "404-with-a-list"])
def test_an_answer_that_is_not_a_model_list_reads_down_and_alerts(fired, answer):
    out = self_check.probe_ollama(lambda path, timeout: answer)
    assert out["ok"] is False, out
    assert fired == ["ollama_down"]
    assert self_check.ollama_readiness() in ("unreachable", "skipped", "off", "stale")
    assert self_check._ollama_last["ok"] is False


@pytest.mark.parametrize("models", [[{"name": "a"}], []], ids=["one-model", "nothing-pulled-yet"])
def test_a_real_model_list_reads_up_with_no_alert(fired, models):
    out = self_check.probe_ollama(lambda path, timeout: _Answer(200, {"models": models}))
    assert out["ok"] is True and fired == []


def test_the_three_readers_share_one_shape_check():
    """/api/status, the Settings Test button and the probe ask the one helper;
    no copy of the shape check is left in either route."""
    from app.routers import settings as settings_router
    from app.routers import system as system_router
    for fn in (system_router.status, settings_router.test_ollama_connection,
               self_check.probe_ollama):
        src = inspect.getsource(fn)
        assert "ollama_model_list(" in src, fn.__name__
        assert '.get("models")' not in src, fn.__name__


@pytest.fixture
def saved_toggle():
    """Set the toggle saved in Settings for one test; put it back after."""
    before = get_config("provider_ollama_enabled", "")

    def _set(value: str):
        set_config("provider_ollama_enabled", value)
    yield _set
    set_config("provider_ollama_enabled", before)


@pytest.mark.parametrize("saved, env, watched", [
    ("false", "true", False),
    ("true", "false", True),
], ids=["off-in-settings-on-in-env", "on-in-settings-off-in-env"])
def test_the_pass_reads_the_toggle_saved_in_settings(
        monkeypatch, fired, saved_toggle, tmp_path, saved, env, watched):
    saved_toggle(saved)
    monkeypatch.setenv("ENABLE_OLLAMA", env)
    out = self_check.run_self_check(str(tmp_path),
                                    lambda path, timeout: _Answer(200, {"models": []}))
    assert ("ollama" in out) is watched, out
    assert self_check.ollama_enabled() is watched


def test_the_timer_follows_the_saved_toggle_pass_by_pass(monkeypatch, fired, saved_toggle):
    """The wiring word the health routes read is decided at boot from the
    saved toggle, then by each pass: a pass that watched Ollama says
    watched, one that did not says skipped."""
    saved_toggle("false")
    monkeypatch.setattr(self_check, "_rag_wired", self_check._rag_wired)
    monkeypatch.setattr(self_check, "_redis_wired", self_check._redis_wired)
    monkeypatch.setattr(self_check, "_started_at", self_check._started_at)
    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0)
    asyncio.run(self_check.self_check_loop(".", lambda path, timeout: None, None))
    assert self_check._ollama_wired is False

    # Each pass notes the word it starts under - the boot's, then what the
    # pass before it decided - and the first watches Ollama, the second not.
    passes = iter([{"disk": {}, "ollama": {"ok": True}}, {"disk": {}}])
    seen = []

    def _pass(*a):
        seen.append(self_check._ollama_wired)
        return next(passes, {"disk": {}})

    async def go():
        task = asyncio.create_task(
            self_check.self_check_loop(".", lambda path, timeout: None, None))
        for _ in range(1000):
            if len(seen) >= 3:
                break
            await asyncio.sleep(0.005)
        task.cancel()

    monkeypatch.setattr(self_check, "SELF_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(self_check, "run_self_check", _pass)
    asyncio.run(go())
    assert seen[:3] == [False, True, False], seen
