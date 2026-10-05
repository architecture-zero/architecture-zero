"""The upload's work runs on a worker thread, not the event loop.

upload_file is async because the body read awaits, and an async route runs ON
the event loop - so until 2026-10-04 everything after that read ran there too:
extraction, the scans, and the embed loop, one model call per chunk. One large
upload stalled every other request on the process until its last chunk landed
(measured that day on an instance running this handler's shape: a file watcher
re-ingesting 207 chunks left /api/health unanswered for ~22 minutes).

Asked directly rather than timed: a running event loop is visible from the
thread that runs it, so the patched steps record whether they were called from
one. A timing test would pass on a fast box with the stall still in.
"""
import asyncio
import threading
import time

from app.routers import kb


def _on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _upload(client, headers, name="offloop.md"):
    return client.post("/api/ingest/upload",
                       files={"file": (name, b"Onboarding notes. Nothing here trips a scanner.",
                                       "text/plain")},
                       data={"department": "general"}, headers=headers)


def test_the_embed_loop_runs_off_the_event_loop(client, admin_headers, monkeypatch):
    seen = []
    monkeypatch.setattr(kb, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(kb, "add_document",
                        lambda *a, **k: seen.append(_on_the_event_loop()))
    r = _upload(client, admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ingested"
    assert seen, "nothing was indexed - the check compared nothing"
    assert not any(seen), "add_document ran on the event loop: one upload stalls every request"


def test_extraction_runs_off_the_event_loop(client, admin_headers, monkeypatch):
    """A large PDF's extraction is seconds of CPU on its own, before a single
    chunk embeds."""
    from app import text_extract
    seen = []
    real = text_extract.extract_text

    def _spy(name, data):
        seen.append(_on_the_event_loop())
        return real(name, data)

    monkeypatch.setattr(text_extract, "extract_text", _spy)
    monkeypatch.setattr(kb, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(kb, "add_document", lambda *a, **k: None)
    r = _upload(client, admin_headers)
    assert r.status_code == 200, r.text
    assert seen == [False], "extraction ran on the event loop"


def test_two_uploads_never_write_at_once(monkeypatch):
    """Off the loop, two uploads can run at once, and two writers interleaving
    set diffs against one index is a pruning race. The write section takes one
    lock across sources - the queued path's single-worker rule - so the two
    files here are different ones: a per-source lock fails this too."""
    active = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def _slow_add(*a, **k):
        with guard:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.2)   # long enough for the other upload to reach its write
        with guard:
            active["now"] -= 1

    monkeypatch.setattr(kb, "_ingest_trust", lambda user, requested=None: "curated")
    monkeypatch.setattr(kb, "get_source_ids", lambda name, dept: [])
    monkeypatch.setattr(kb, "add_document", _slow_add)
    monkeypatch.setattr(kb, "resolve_moot_holds", lambda *a, **k: 0)
    results, errors = [], []

    def _one(name):
        try:
            results.append(kb._ingest_upload(name, "md", b"Onboarding notes.",
                                             "general", {}))
        except Exception as e:   # surfaced by the assert below, not swallowed
            errors.append(repr(e))

    threads = [threading.Thread(target=_one, args=(f"concurrent-{i}.md",))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert [r["status"] for r in results] == ["ingested", "ingested"]
    assert active["peak"] == 1, "two uploads wrote to the index at once"
