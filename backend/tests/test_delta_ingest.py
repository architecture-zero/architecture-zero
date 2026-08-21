"""Per-entry (content-addressed delta) ingestion.

Chunk ids are md5(dept::name::chunk-text), so _ingest_file diffs the desired
set against the index: only new/changed text embeds, only removed text deletes.
These pin the four behaviors that matter: first ingest embeds everything,
prepending a log entry embeds ONLY it, editing swaps exactly the edited chunk,
and a retry after mid-file failure resumes instead of starting over.
"""
import hashlib

import app.main as main_mod

# The default HISTORY_SOURCES entry (app/rag_config.py) - routed to the
# history department, where chunking is dated-section-aware.
LOG_NAME = "internal/session-log.md"

ENTRY_A = "## 2031-03-05 - first entry\n\nRe-slotted the seasonal aisle.\n"
ENTRY_B = "## 2031-03-06 - second entry\n\nSecond pack-out scan piloted.\n"
ENTRY_C = "## 2031-03-07 - third entry\n\nCycle count recount cleared.\n"


def _expected_ids(text, dept="history"):
    from app.chunking import chunk_dated_markdown
    return {
        hashlib.md5(f"{dept}::{LOG_NAME}::{p['text']}".encode(),
                    usedforsecurity=False).hexdigest()
        for p in chunk_dated_markdown(text)
    }


class Harness:
    """Captures the index interactions; `index` simulates what chroma holds."""

    def __init__(self, monkeypatch, preexisting_ids=()):
        self.index = set(preexisting_ids)
        self.added = []
        self.deleted = []
        monkeypatch.setattr(main_mod, "get_source_ids",
                            lambda source, dept=None: list(self.index))
        monkeypatch.setattr(main_mod, "delete_documents",
                            lambda ids, dept=None: (self.deleted.extend(ids),
                                                    self.index.difference_update(ids)))
        monkeypatch.setattr(main_mod, "delete_source", lambda source, dept=None: None)
        monkeypatch.setattr(main_mod, "add_document",
                            lambda doc_id, text, meta, department=None:
                            (self.added.append(doc_id), self.index.add(doc_id)))

        # _ingest_file writes new chunks through the BATCH shape (one embed
        # round trip per slice); the harness records the same per-chunk facts
        # either way, so every delta assertion below stays byte-identical.
        def _batch(entries, department=None, quarantine_exempt=False):
            for doc_id, _text, _meta in entries:
                self.added.append(doc_id)
                self.index.add(doc_id)
            return len(entries)
        monkeypatch.setattr(main_mod, "add_documents_batch", _batch)
        monkeypatch.setattr(main_mod, "_load_ingest_state", lambda: {})
        monkeypatch.setattr(main_mod, "_save_ingest_state", lambda state: None)


def test_ingest_file_uses_the_batch_write_shape():
    # exists != wired: a batch write path can land in the codebase while the
    # file delta path stays serial. The delta path must write through the
    # batch shape, or every changed file pays one embed round trip per chunk.
    import inspect
    src = inspect.getsource(main_mod._ingest_file)
    assert "add_documents_batch(" in src
    assert "add_document(" not in src.replace("add_documents_batch(", "")


def test_first_ingest_embeds_everything(monkeypatch):
    h = Harness(monkeypatch)
    text = ENTRY_A + "\n" + ENTRY_B
    main_mod._ingest_file(LOG_NAME, text)
    assert set(h.added) == _expected_ids(text)
    assert h.deleted == []


def test_prepending_an_entry_embeds_only_it(monkeypatch):
    old_text = ENTRY_A + "\n" + ENTRY_B
    h = Harness(monkeypatch, preexisting_ids=_expected_ids(old_text))
    new_text = ENTRY_C + "\n" + old_text  # newest-on-top, like a real log
    main_mod._ingest_file(LOG_NAME, new_text)
    assert set(h.added) == _expected_ids(new_text) - _expected_ids(old_text), \
        "only the new entry's chunks may embed"
    assert h.deleted == [], "existing entries must not re-embed or be dropped"


def test_editing_an_entry_swaps_exactly_it(monkeypatch):
    old_text = ENTRY_A + "\n" + ENTRY_B
    h = Harness(monkeypatch, preexisting_ids=_expected_ids(old_text))
    edited_b = ENTRY_B.replace("92 percent", "92 percent (see the first run)")
    new_text = ENTRY_A + "\n" + edited_b
    main_mod._ingest_file(LOG_NAME, new_text)
    assert set(h.added) == _expected_ids(new_text) - _expected_ids(old_text)
    assert set(h.deleted) == _expected_ids(old_text) - _expected_ids(new_text)
    # entry A untouched in both directions
    assert _expected_ids(ENTRY_A) & (set(h.added) | set(h.deleted)) == set()


def test_retry_after_partial_failure_resumes(monkeypatch):
    text = ENTRY_A + "\n" + ENTRY_B + "\n" + ENTRY_C
    all_ids = _expected_ids(text)
    partially_landed = set(list(sorted(all_ids))[:1])  # one chunk made it, then a crash
    h = Harness(monkeypatch, preexisting_ids=partially_landed)
    main_mod._ingest_file(LOG_NAME, text)
    assert set(h.added) == all_ids - partially_landed, \
        "retry must embed only what is missing"
    assert h.deleted == []


def test_expected_chunk_count_matches_ingest_dedup(monkeypatch):
    # A duplicated section collapses to one id in _ingest_file; the count
    # helper must agree or the skip check would re-ingest such files forever.
    text = ENTRY_A + "\n" + ENTRY_B + "\n" + ENTRY_A
    h = Harness(monkeypatch)
    main_mod._ingest_file(LOG_NAME, text)
    assert len(h.index) == main_mod._expected_chunk_count(LOG_NAME, text)


def test_startup_skip_requires_full_count(monkeypatch, tmp_path):
    # Fingerprint unchanged + source present BUT the index is short (the
    # metadata-wipe class: a wipe can leave a source a few chunks of hundreds
    # with the file untouched) - the file must re-ingest, not skip. Presence
    # is not completeness.
    f = tmp_path / "note.md"
    text = "## One\n\nalpha.\n\n## Two\n\nbeta.\n"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setattr(main_mod, "KNOWLEDGE_DIR", str(tmp_path))
    fp = main_mod._ingest_fingerprint("note.md", text)
    monkeypatch.setattr(main_mod, "_load_ingest_state", lambda: {"note.md": fp})
    calls = []
    monkeypatch.setattr(main_mod, "_ingest_file",
                        lambda name, t: calls.append(name) or 2)
    expected = main_mod._expected_chunk_count("note.md", text)

    monkeypatch.setattr(main_mod, "list_sources", lambda department=None: [
        {"source": "note.md", "count": expected, "department": "general"}])
    res = main_mod._sync_knowledge_dir(force=False)
    assert res["note.md"]["status"] == "skipped" and calls == []

    monkeypatch.setattr(main_mod, "list_sources", lambda department=None: [
        {"source": "note.md", "count": expected - 1, "department": "general"}])
    res = main_mod._sync_knowledge_dir(force=False)
    assert res["note.md"]["status"] == "ok" and calls == ["note.md"]


def test_watcher_replace_event_does_not_mass_delete(monkeypatch, tmp_path):
    # git checkout swaps files via unlink+rename, so watchfiles reports
    # DELETED for a file that still exists - the metadata-wipe root cause.
    # A replace must delta re-ingest, never delete_source.
    f = tmp_path / "note.md"
    f.write_text("## One\n\nalpha.\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "KNOWLEDGE_DIR", str(tmp_path))
    deleted_sources = []
    monkeypatch.setattr(main_mod, "delete_source",
                        lambda source, dept=None: deleted_sources.append(source))
    ingested = []
    monkeypatch.setattr(main_mod, "_ingest_file",
                        lambda name, t: ingested.append(name) or 1)
    main_mod._handle_watched_change(True, f)  # "deleted" event, file on disk
    assert deleted_sources == []
    assert ingested == ["note.md"]


def test_watcher_true_delete_still_deletes(monkeypatch, tmp_path):
    f = tmp_path / "gone.md"  # never created on disk
    monkeypatch.setattr(main_mod, "KNOWLEDGE_DIR", str(tmp_path))
    deleted_sources = []
    monkeypatch.setattr(main_mod, "delete_source",
                        lambda source, dept=None: deleted_sources.append(source))
    monkeypatch.setattr(main_mod, "_load_ingest_state", lambda: {})
    monkeypatch.setattr(main_mod, "_save_ingest_state", lambda s: None)
    ingested = []
    monkeypatch.setattr(main_mod, "_ingest_file",
                        lambda name, t: ingested.append(name))
    main_mod._handle_watched_change(True, f)
    assert "gone.md" in deleted_sources
    assert ingested == []


def test_general_docs_also_delta(monkeypatch):
    h = Harness(monkeypatch)
    name = "company/services.md"
    text = ("## Ground freight\n\nFive to seven business days.\n\n"
            "## Claims\n\nFiled within fourteen days of delivery.\n")
    main_mod._ingest_file(name, text)
    first = list(h.added)
    assert len(first) > 0
    # re-ingest identical content: nothing embeds, nothing deletes
    h.added.clear()
    main_mod._ingest_file(name, text)
    assert h.added == [] and h.deleted == []
