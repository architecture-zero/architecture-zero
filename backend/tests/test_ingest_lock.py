"""The per-source ingest lock.

A per-source ingest is not atomic - it purges general-dept residue, diffs chunk
ids, then writes - and the watcher and the manual sync can reach the same
source at once, so two of them can interleave and one can delete what the
other just wrote. Concurrent writers on one collection are also the shape that
corrupts an HNSW index.

These pin what makes the lock worth having and safe to hold: one writer per
source but never one writer in all, the watcher's delete and the two purges
behind the same lock, and the watcher never taking it on the event loop.
"""
import asyncio
import enum
import pathlib
import sys
import threading
import time
import types

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


def test_one_source_name_is_one_lock():
    a = ingest_sync._source_lock("docs/a.md")
    assert ingest_sync._source_lock("docs/a.md") is a
    assert ingest_sync._source_lock("docs/b.md") is not a


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
