"""The suite's TestClient starts no boot sync (2026-10-29).

startup_tasks launches the boot's background sync with asyncio.create_task:
the SQL-only stages, chroma maintenance, the knowledge and docs syncs with
their orphan prunes, the help pages and system records, then the knowledge
watcher. Under the session client those ran in executor threads against
whatever a test had monkeypatched at that moment, so the admin prune
endpoint's dry run in test_knowledge_orphan_prune.py could see the boot's
docs prune take its patched docs/GONE.md (a full run hid it, the boot having
long finished by then). conftest sets BOOT_BACKGROUND_SYNC=false. These pin
that the switch holds under the suite, that a test can opt back in, and that
a host which never set it boots as it always has.
"""
import asyncio
import types

import pytest

from app import main, runtime_config

# What the boot sync leaves pending in the app's loop: its own task while it
# runs, the knowledge watcher's after it. The last test below pins the first
# name from the real create_task call, so a rename cannot leave this checking
# for a name that no longer exists.
_BOOT_SYNC = "startup_tasks.<locals>._bg_guarded"
_WATCHER = "_watch_knowledge_dir"
# The self-check timer starts from the same hook and is not part of the boot
# sync: it starts whatever the switch says.
_SELF_CHECK = "self_check_loop"


def _pending_coroutines(client) -> list[str]:
    def _names():
        return [getattr(t.get_coro(), "__qualname__", "") for t in asyncio.all_tasks()]
    return client.portal.call(_names)


def test_the_suite_client_started_no_boot_sync_and_no_watcher(client):
    pending = _pending_coroutines(client)
    assert _BOOT_SYNC not in pending and _WATCHER not in pending, pending
    assert runtime_config._startup_ingest_active is False


@pytest.mark.parametrize("value, on", [
    (None, True), ("true", True), ("TRUE", True), ("off", True), ("0", True),
    ("flase", True), ("false", False), ("  False ", False)])
def test_the_switch_is_on_unless_set_to_false(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("BOOT_BACKGROUND_SYNC", raising=False)
    else:
        monkeypatch.setenv("BOOT_BACKGROUND_SYNC", value)
    assert main._boot_sync_enabled() is on


@pytest.mark.parametrize("value", [None, "true", "false"])
def test_startup_starts_the_boot_sync_only_with_the_switch_on(monkeypatch, value):
    """The opt-in path: a test that needs the boot sync sets the variable and
    startup starts it. Only the decision runs here - each task's coroutine is
    recorded and closed, never run."""
    if value is None:
        monkeypatch.delenv("BOOT_BACKGROUND_SYNC", raising=False)
    else:
        monkeypatch.setenv("BOOT_BACKGROUND_SYNC", value)
    started, errors = [], []

    def _record(coro, **kwargs):
        started.append(coro.__qualname__)
        coro.close()

    monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(create_task=_record))
    monkeypatch.setattr(main, "log_error", lambda event, **kw: errors.append(event))
    monkeypatch.setattr(runtime_config, "_startup_ingest_active", False)

    asyncio.run(main.startup_tasks())

    if value == "false":
        # No boot task, no flag armed (the instance never reads as mid-ingest),
        # and said at error level rather than silently.
        assert started == [_SELF_CHECK]
        assert runtime_config._startup_ingest_active is False
        assert "boot_background_sync_off" in errors
    else:
        assert started == [_BOOT_SYNC, _SELF_CHECK]
        assert runtime_config._startup_ingest_active is True
        assert "boot_background_sync_off" not in errors
