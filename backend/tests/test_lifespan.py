"""The app starts and stops through one lifespan handler (2026-11-22).

FastAPI 0.162 keeps `@app.on_event` but marks it deprecated (starlette 1.0
removed its own), and an app given a lifespan never runs a hook registered
that way: FastAPI's default lifespan was what ran them, and the app's own
replaces it. A hook registered the old way would sit there and never run,
with nothing to say so. These hold the move:

  - the handler runs every startup hook, in STARTUP_HOOKS' order, before the
    app serves, and every shutdown hook when it stops;
  - the shutdown hooks run on an exit by error too, and a failed startup hook
    stops the boot with no shutdown hook run: both as FastAPI's own default
    did (its _DefaultLifespan);
  - a plain function hook runs as well as a coroutine, as it did;
  - the order is the one the decorators registered before the move;
  - nothing registers a hook the deprecated way;
  - the app the suite's client and uvicorn start runs this handler.
"""
import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

APP_DIR = Path(main.__file__).resolve().parent

# The order the @app.on_event("startup") decorators registered them in, top
# of main.py down, before the move. The boot stamp stays first: a boot that
# dies later must already be counted (test_boot_history.py).
STARTUP_ORDER = [
    "_record_boot_on_startup",
    "_claim_code_on_startup",
    "_require_mfa_on_startup",
    "_on_off_settings_on_startup",
    "_system_prompt_divergence_on_startup",
    "startup_tasks",
]
SHUTDOWN_ORDER = ["_flush_chroma_on_shutdown"]


def _hook(calls, name, fail=False):
    async def hook():
        calls.append(name)
        if fail:
            raise RuntimeError(name)
    return hook


def _plain_hook(calls, name):
    def hook():
        calls.append(name)
    return hook


def _run_lifespan(body=None):
    async def run():
        async with main._lifespan(main.app):
            if body:
                body()
    asyncio.run(run())


def test_startup_hooks_run_in_order_before_serving_then_shutdown(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "STARTUP_HOOKS", (_hook(calls, "first"), _hook(calls, "second")))
    monkeypatch.setattr(main, "SHUTDOWN_HOOKS", (_hook(calls, "stop"),))
    _run_lifespan(lambda: calls.append("serving"))
    assert calls == ["first", "second", "serving", "stop"]


def test_a_plain_function_hook_runs_too(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "STARTUP_HOOKS", (_plain_hook(calls, "plain"), _hook(calls, "async")))
    monkeypatch.setattr(main, "SHUTDOWN_HOOKS", (_plain_hook(calls, "plain stop"),))
    _run_lifespan()
    assert calls == ["plain", "async", "plain stop"]


def test_the_shutdown_hooks_run_on_an_exit_by_error(monkeypatch):
    """FastAPI's default ran them in __aexit__, whatever the exit."""
    calls = []
    monkeypatch.setattr(main, "STARTUP_HOOKS", (_hook(calls, "up"),))
    monkeypatch.setattr(main, "SHUTDOWN_HOOKS", (_hook(calls, "stop"),))

    def stops_by_error():
        raise ConnectionError("the server stopped by error")
    with pytest.raises(ConnectionError):
        _run_lifespan(stops_by_error)
    assert calls == ["up", "stop"]


def test_a_failed_startup_hook_stops_the_boot_and_runs_no_shutdown_hook(monkeypatch):
    """FastAPI's default ran the startup hooks in __aenter__: one that raised
    stopped the boot there, and __aexit__ never ran."""
    calls = []
    monkeypatch.setattr(main, "STARTUP_HOOKS", (_hook(calls, "breaks", fail=True), _hook(calls, "never")))
    monkeypatch.setattr(main, "SHUTDOWN_HOOKS", (_hook(calls, "stop"),))
    with pytest.raises(RuntimeError, match="breaks"):
        _run_lifespan(lambda: calls.append("serving"))
    assert calls == ["breaks"]


def test_the_hooks_and_their_order():
    assert [h.__name__ for h in main.STARTUP_HOOKS] == STARTUP_ORDER
    assert [h.__name__ for h in main.SHUTDOWN_HOOKS] == SHUTDOWN_ORDER
    for name in STARTUP_ORDER + SHUTDOWN_ORDER:
        assert getattr(main, name) in main.STARTUP_HOOKS + main.SHUTDOWN_HOOKS


def test_no_hook_is_registered_the_deprecated_way():
    """On the router: include_router copies an included router's hooks onto
    the app's, so empty lists here cover the routers too. And in the source,
    for a router included somewhere this import did not reach."""
    assert main.app.router.on_startup == []
    assert main.app.router.on_shutdown == []
    pattern = re.compile(r"\.on_event\(|\badd_event_handler\(|\bon_startup\s*=|\bon_shutdown\s*=")
    found = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                found.append(f"{path.relative_to(APP_DIR)}:{n}: {line.strip()}")
    assert found == [], "register it in main.STARTUP_HOOKS or SHUTDOWN_HOOKS:\n" + "\n".join(found)


def test_the_app_starts_and_stops_through_this_handler(monkeypatch):
    """The suite's client, like uvicorn, starts the app through its lifespan:
    entered as a context manager, it runs what the two tuples name."""
    calls = []
    monkeypatch.setattr(main, "STARTUP_HOOKS", (_hook(calls, "up"),))
    monkeypatch.setattr(main, "SHUTDOWN_HOOKS", (_hook(calls, "down"),))
    with TestClient(main.app):
        assert calls == ["up"]
    assert calls == ["up", "down"]
