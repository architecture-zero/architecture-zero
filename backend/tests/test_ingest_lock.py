"""The per-source ingest lock.

A per-source ingest is not atomic - it purges general-dept residue, diffs chunk
ids, then writes - and the watcher and the manual sync can reach the same
source at once, so two of them can interleave and one can delete what the
other just wrote. Concurrent writers on one collection are also the shape that
corrupts an HNSW index.

These pin what makes the lock worth having and safe to hold: one writer per
source but never one writer in all, the watcher's delete and the two purges
behind the same lock, the watcher never taking it on the event loop - and,
since 2026-10-29, the registry holding only the names with work in flight.
"""
import asyncio
import enum
import pathlib
import sys
import threading
import time
import types

import pytest

from app import ingest_sync

TEXT = "## One\n\nalpha.\n\n## Two\n\nbeta.\n"


def _stub_index(monkeypatch, batch, delete_source=lambda source, dept=None: None):
    """The vector store's seams around the real ingest: every chunk is new,
    the batch write is the caller's, and the ingest state stays in memory."""
    monkeypatch.setattr(ingest_sync, "get_source_ids", lambda source, dept=None: [])
    monkeypatch.setattr(ingest_sync, "add_documents_batch", batch)
    monkeypatch.setattr(ingest_sync, "delete_documents", lambda ids, dept=None: None)
    monkeypatch.setattr(ingest_sync, "delete_source", delete_source)
    monkeypatch.setattr(ingest_sync, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_sync, "_save_ingest_state", lambda state: None)


def _held(source):
    """Whether this source's lock is held right now (no registry reads as no)."""
    lock = getattr(ingest_sync, "_ingest_locks", {}).get(source)
    return bool(lock and lock.locked())


def _in_table(source):
    """Whether the registry holds an entry for this source right now."""
    return (source in getattr(ingest_sync, "_ingest_locks", {})
            or source in getattr(ingest_sync, "_ingest_lock_waiters", {}))


class _Writers:
    """Counts the writers inside the batch write at once, per source and in all."""

    def __init__(self):
        self._guard = threading.Lock()
        self.inside, self.most = {}, {}
        self.inside_all = self.most_all = 0

    def batch(self, entries, department=None, quarantine_exempt=False):
        source = entries[0][2]["source"]
        with self._guard:
            self.inside[source] = self.inside.get(source, 0) + 1
            self.most[source] = max(self.most.get(source, 0), self.inside[source])
            self.inside_all += 1
            self.most_all = max(self.most_all, self.inside_all)
        self.while_inside(source)
        with self._guard:
            self.inside[source] -= 1
            self.inside_all -= 1
        return len(entries)

    def while_inside(self, source):
        time.sleep(0.05)


def _ingest_together(names):
    """One _ingest_file per name, all released at the same instant."""
    start = threading.Barrier(len(names))

    def run(name):
        start.wait()
        ingest_sync._ingest_file(name, TEXT)

    threads = [threading.Thread(target=run, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "an ingest never finished"


def test_two_writers_on_one_source_take_turns(monkeypatch):
    writers = _Writers()
    _stub_index(monkeypatch, writers.batch)
    _ingest_together(["notes/same.md"] * 4)
    assert writers.most == {"notes/same.md": 1}, "two writers were inside one source at once"


def test_different_sources_do_not_wait_for_each_other(monkeypatch):
    """PER-source, not global: one global lock would serialise a whole sync
    behind its slowest file for no safety gain. Each writer waits inside its
    write until the other source's writer is inside too - which a per-source
    lock allows and a global one times out."""
    entered = {"a.md": threading.Event(), "b.md": threading.Event()}
    met = []

    class Meeting(_Writers):
        def while_inside(self, source):
            entered[source].set()
            other = "b.md" if source == "a.md" else "a.md"
            met.append(entered[other].wait(timeout=2))
            time.sleep(0.05)

    writers = Meeting()
    _stub_index(monkeypatch, writers.batch)
    _ingest_together(["a.md", "a.md", "b.md", "b.md"])
    assert writers.most == {"a.md": 1, "b.md": 1}, "two writers were inside one source at once"
    assert all(met) and writers.most_all == 2, "one source's writer waited on another's"


def test_two_holds_of_one_name_share_one_entry_until_the_last_releases():
    """One entry per name in flight: the second hold waits on the Lock the
    first holds (until 2026-10-29 this test pinned that as `_source_lock`
    returning the same Lock object twice; a hold is a context manager now, so
    the sharing shows as the wait). The entry stays while either is in flight
    - dropped under a waiter, the next arrival would get a fresh Lock and the
    source two writers - and goes when the last one out releases. A different
    name is a different entry: its hold enters beside this one."""
    name, other = "docs/in-flight.md", "docs/other-name.md"
    first_in, first_go = threading.Event(), threading.Event()
    second_in, second_go = threading.Event(), threading.Event()
    other_in, other_go = threading.Event(), threading.Event()

    def hold(source, entered, go):
        with ingest_sync._source_lock(source):
            entered.set()
            go.wait(timeout=5)

    first = threading.Thread(target=hold, args=(name, first_in, first_go))
    second = threading.Thread(target=hold, args=(name, second_in, second_go))
    other_hold = threading.Thread(target=hold, args=(other, other_in, other_go),
                                  daemon=True)   # a global lock would hang it here
    first.start()
    assert first_in.wait(timeout=5)
    second.start()
    assert not second_in.wait(timeout=0.3), "the second hold entered beside the first"
    assert _held(name)
    other_hold.start()
    assert other_in.wait(timeout=2), "a different name waited on this one"
    first_go.set()
    assert second_in.wait(timeout=5), "the second hold never got the lock the first released"
    first.join(timeout=5)
    assert _held(name) and _in_table(name), "the entry went while the second hold was inside"
    second_go.set()
    second.join(timeout=5)
    assert not _in_table(name), "the last hold out left the entry behind"
    other_go.set()
    other_hold.join(timeout=5)
    assert not _in_table(other)


def test_released_names_leave_the_table():
    """The registry kept one Lock per name ever handed to it, for the life of
    the process, until 2026-10-29: every sync, upload and delete added one -
    deleted names and names that never existed included - so a long-lived
    process grew by one Lock per name it had ever seen. The table holds only
    names with work in flight: lock and release N fresh names, and none
    remain."""
    names = [f"docs/released-{i}.md" for i in range(25)]
    for n in names:
        with ingest_sync._source_lock(n):
            assert _held(n)
    left = [n for n in names if _in_table(n)]
    assert left == [], f"{len(left)} of {len(names)} released names still in the table"


def test_a_hold_that_raises_still_releases_and_drops_its_entry():
    name = "docs/raises.md"
    with pytest.raises(RuntimeError):
        with ingest_sync._source_lock(name):
            raise RuntimeError("mid-write")
    assert not _in_table(name)
    with ingest_sync._source_lock(name):    # and the name is free for the next writer
        assert _held(name)
    assert not _in_table(name)


def test_a_watcher_delete_of_an_unknown_name_leaves_no_entry(monkeypatch, tmp_path):
    """The leak's cheapest vector: a delete of a name never ingested - nothing
    on disk, nothing in the index - took the lock and left its Lock behind."""
    monkeypatch.setattr(ingest_sync, "KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_sync, "_DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(ingest_sync, "_DOCS_ROOT_FILES", [])
    monkeypatch.setattr(ingest_sync, "delete_source", lambda source, dept=None: None)
    monkeypatch.setattr(ingest_sync, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_sync, "_save_ingest_state", lambda state: None)
    ingest_sync._handle_watched_change(True, tmp_path / "never-there.md")
    assert not _in_table("never-there.md")


def _fake_watchfiles(changes):
    """A watchfiles stand-in whose awatch yields one batch of changes and stops."""
    module = types.ModuleType("watchfiles")

    class Change(enum.IntEnum):
        added = 1
        modified = 2
        deleted = 3

    async def awatch(*paths, **kwargs):
        yield [(getattr(Change, kind), str(path)) for kind, path in changes]

    module.Change, module.awatch = Change, awatch
    return module


def _on_the_event_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_the_watcher_handles_changes_off_the_event_loop(monkeypatch, tmp_path):
    """The lock is only safe to hold because the watcher hops to an executor
    first: a blocking acquire on the loop, while a sync holds the lock, would
    stall every request for the length of that ingest. Still awaited, so a
    batch's changes are handled one at a time, in order."""
    seen = []

    def handle(deleted, p):
        seen.append(("start", p.name, _on_the_event_loop()))
        time.sleep(0.05)
        seen.append(("end", p.name, _on_the_event_loop()))

    monkeypatch.setitem(sys.modules, "watchfiles", _fake_watchfiles(
        [("added", tmp_path / "first.md"), ("modified", tmp_path / "second.md")]))
    monkeypatch.setattr(ingest_sync, "KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_sync, "_DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(ingest_sync, "_DOCS_ROOT_FILES", [])
    monkeypatch.setattr(ingest_sync, "_handle_watched_change", handle)
    asyncio.run(ingest_sync._watch_knowledge_dir())
    assert not any(on_loop for _, _, on_loop in seen), "the watcher handled a change on the event loop"
    assert [step[:2] for step in seen] == [("start", "first.md"), ("end", "first.md"),
                                          ("start", "second.md"), ("end", "second.md")]


def test_the_watcher_delete_waits_for_an_in_flight_ingest(monkeypatch, tmp_path):
    """A file deleted while a sync is still writing it: the delete must land
    after that write, not inside it - otherwise the write finishes after the
    delete and the index keeps chunks of a file that is gone."""
    events = []
    writing, release = threading.Event(), threading.Event()

    def batch(entries, department=None, quarantine_exempt=False):
        events.append("write-start")
        writing.set()
        release.wait(timeout=5)
        events.append("write-end")
        return len(entries)

    _stub_index(monkeypatch, batch,
                delete_source=lambda source, dept=None: events.append("delete"))
    monkeypatch.setattr(ingest_sync, "KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_sync, "_DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(ingest_sync, "_DOCS_ROOT_FILES", [])
    ingest = threading.Thread(target=ingest_sync._ingest_file, args=("note.md", TEXT))
    ingest.start()
    assert writing.wait(timeout=5)
    # note.md is not on disk, so the event is a true delete, not a replace.
    delete = threading.Thread(target=ingest_sync._handle_watched_change,
                              args=(True, tmp_path / "note.md"))
    delete.start()
    delete.join(timeout=0.5)    # an unlocked delete lands now, inside the write
    release.set()
    for t in (ingest, delete):
        t.join(timeout=5)
    assert events == ["write-start", "write-end", "delete"]


def test_the_docs_orphan_prune_rechecks_under_the_lock(monkeypatch, tmp_path):
    """The prune reads which files exist, lists the index, then deletes. A file
    written back in between is the watcher's to ingest, so each delete holds
    its source's lock and re-checks the disk first. A true orphan still goes."""
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(ingest_sync, "_DOCS_DIR", docs)
    monkeypatch.setattr(ingest_sync, "_DOCS_ROOT_FILES", [])

    def listing(department=None):
        (docs / "back.md").write_text(TEXT, encoding="utf-8")   # back after the disk read
        return [{"source": "docs/back.md", "department": "general", "count": 2},
                {"source": "docs/gone.md", "department": "general", "count": 2}]

    deleted = []
    monkeypatch.setattr(ingest_sync, "list_sources", listing)
    monkeypatch.setattr(ingest_sync, "delete_source",
                        lambda source, dept=None: deleted.append((source, _held(source))))
    out = ingest_sync._prune_orphan_docs()
    assert deleted == [("docs/gone.md", True)]
    assert [d["source"] for d in out["deleted"]] == ["docs/gone.md"]


def test_the_legacy_key_purge_rechecks_under_the_lock(monkeypatch, tmp_path):
    """The knowledge sync purges a subdir file's old basename key unless a
    top-level file owns that name - judged from a listing taken before the
    loop. A top-level file written since is the watcher's to ingest, so the
    purge holds the key's lock and re-checks the disk first. A key no file
    owns still goes."""
    (tmp_path / "sub").mkdir()
    readme = tmp_path / "sub" / "README.md"
    readme.write_text(TEXT, encoding="utf-8")
    (tmp_path / "sub" / "other.md").write_text(TEXT, encoding="utf-8")
    real_read_text = pathlib.Path.read_text

    def read_text(self, *args, **kwargs):
        if self == readme:   # a top-level README.md lands mid-sync
            (tmp_path / "README.md").write_text(TEXT, encoding="utf-8")
        return real_read_text(self, *args, **kwargs)

    deleted = []

    def record(source, dept=None):
        deleted.append((source, _held(source)))

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    monkeypatch.setattr(ingest_sync, "KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_sync, "_ingest_file", lambda name, text: 2)
    # Whichever delete the purge reaches for: which chunks it may take is
    # test_legacy_key_purge.py's question, this one asks only about the lock.
    monkeypatch.setattr(ingest_sync, "delete_source", record)
    monkeypatch.setattr(ingest_sync, "delete_file_chunks", record)
    monkeypatch.setattr(ingest_sync, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(ingest_sync, "_save_ingest_state", lambda state: None)
    ingest_sync._sync_knowledge_dir(force=True)
    assert deleted == [("other.md", True)]
