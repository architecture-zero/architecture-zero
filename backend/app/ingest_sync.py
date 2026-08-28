"""Knowledge-directory ingest: change detection, the file watcher, docs sync.

Not a router - no APIRouter, no routes. This is the subsystem the startup hooks
drive and the KB routes reuse, lifted out of main.py so both callers import it
from one place.

Direction is one-way: main -> ingest_sync, and main -> routers.kb -> ingest_sync.
Nothing here imports app.main.

_ingest_file and _sync_docs moved byte-for-byte and must stay that way. Three
tests read them through inspect.getsource and assert on literal substrings and
their ORDER - that add_documents_batch(new_entries appears before
delete_documents(sorted(stale), that the token add_document( never appears, and
that "docs_orphan_prune_failed" survives. Do not reflow them, do not rename
their locals, and do not run a formatter over this file.
"""
import os
import json
import hashlib
import logging
import pathlib

from app.chunking import (chunk_dated_markdown, chunk_markdown_sections,
                          CHUNKER_VERSION)
from app.database import (add_documents_batch, list_sources, delete_source,
                          get_source_ids, delete_documents)
from app.logger import log, log_error
from app.rag_config import dept_for_source as _dept_for_source

logger = logging.getLogger(__name__)


KNOWLEDGE_DIR                = os.path.abspath(os.getenv("KNOWLEDGE_DIR", "../knowledge"))
_DOCS_DIR                    = pathlib.Path(os.getenv("DOCS_DIR", "/app/docs"))
# Extra root-level files ingested alongside docs/ (comma-separated absolute
# paths) - a deploy that wants its PLAN/README in the corpus names them here.
_DOCS_ROOT_FILES             = [pathlib.Path(p.strip()) for p in
                                os.getenv("DOCS_ROOT_FILES", "").split(",") if p.strip()]
_WATCHED_EXTS                = {".md", ".txt", ".pdf", ".py", ".js", ".ts", ".json", ".yaml", ".yml"}
# The session log is HISTORY, not current fact: a long-running log absorbs
# fact answers and crowds curated docs out of the default candidate pool.
# Sources mapped to history (rag_config.HISTORY_SOURCES) ingest into the
# kb_history department instead of general; the query router (app/routing.py,
# applied inside rerank.retrieve) pulls the history pool back in for
# history-shaped questions only. The source->department map lives in
# app/rag_config (dept_for_source) so retrieval AND the file-tool gate share
# ONE mapping; imported as _dept_for_source below.

# -- Ingest change-detection --------------------------------------------------
# Startup must not re-embed the ENTIRE corpus on every boot - on a
# no-headroom box that burst load is what a freeze is made of. Each
# successful ingest records a fingerprint (content + chunker version +
# department); the startup sync skips files whose fingerprint is unchanged.
# The manual /api/kb/sync stays force=True (full-rebuild semantics
# preserved). Lives on the persistent data volume, beside Chroma - and a skip
# additionally requires the indexed chunk COUNT to match the file's expected
# count (_expected_chunk_count - mere presence lets a partially wiped source
# skip forever), so a wiped OR partially wiped Chroma with a surviving state
# file re-ingests instead of silently staying empty.
_INGEST_STATE_PATH = os.path.join(
    os.getenv("CHROMA_PATH", os.path.join(os.path.dirname(__file__), "..", "data")),
    "ingest-state.json")


def _load_ingest_state() -> dict:
    try:
        with open(_INGEST_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ingest_state(state: dict) -> None:
    """Best-effort atomic write; change-detection is an optimization, never a
    correctness dependency, so failures only mean extra re-embedding."""
    try:
        tmp = _INGEST_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _INGEST_STATE_PATH)
    except Exception as e:
        logging.getLogger("uvicorn.error").warning("ingest-state save failed: %s", e)


def _clear_ingest_fingerprints(sources) -> None:
    """Drop sources from the ingest state so the next sync re-ingests them.
    Content-addressing then re-embeds only the chunks actually missing from
    the index - this is how index-healing maintenance hands its work to the
    syncs."""
    state = _load_ingest_state()
    changed = False
    for s in sources:
        if state.pop(s, None) is not None:
            changed = True
    if changed:
        _save_ingest_state(state)


def _ingest_fingerprint(name: str, text: str) -> str:
    key = f"{CHUNKER_VERSION}::{_dept_for_source(name)}::{text}"
    return hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()


from app.rag_config import dept_for_source as _dept_for_source  # noqa: E402  (map lives in rag_config, shared with the file-tool gate)


def _ingest_file(name: str, text: str) -> int:
    """DELTA-ingest a single file into its department.

    Chunk ids are CONTENT-ADDRESSED - md5(dept::name::chunk-text) - not
    positional. Ingesting is a set-diff against the index: embed only chunks
    whose text is new, delete only chunks whose text is gone. Prepending one
    session-log entry embeds a handful of chunks instead of the whole file.
    Rollout needs no migration: unchanged files stay fingerprint-skipped; a
    file's first change after this ships does one last full swap (its old
    positional ids match nothing), then deltas forever. Identical chunk texts
    within one file collapse to one stored chunk (id equality = dedup,
    acceptable).
    """
    dept = _dept_for_source(name)
    if dept != "general":
        # Self-healing migration: purge chunks this source left in general
        # before it was department-routed (first deploy, or any future map
        # change).
        delete_source(name, "general")
    if dept == "history":
        # Structure-aware: chunk on dated '## ' entries and stamp entry_date
        # so retrieval can recency-weight within the history pool.
        parts = chunk_dated_markdown(text)
    else:
        # General markdown chunks on '## ' sections too (dense single-topic
        # chunks; no entry_date - fact docs must not decay).
        parts = [{"text": c, "entry_date": None} for c in chunk_markdown_sections(text)]
    desired: dict[str, tuple[int, dict]] = {}
    for i, part in enumerate(parts):
        doc_id = hashlib.md5(f"{dept}::{name}::{part['text']}".encode(),
                             usedforsecurity=False).hexdigest()
        desired[doc_id] = (i, part)
    existing = set(get_source_ids(name, dept))
    stale = existing - desired.keys()
    # BATCHED: the new chunks go through add_documents_batch - the SAME gate
    # per chunk, then one embed call + one upsert per EMBED_BATCH_SIZE slice
    # (one embed round trip PER CHUNK makes a boot re-ingest serial and
    # slow). Delta semantics unchanged: only new chunk ids reach the batch.
    new_entries = []
    for doc_id, (i, part) in desired.items():
        if doc_id in existing:
            continue  # unchanged chunk: already embedded, never re-embed it
        meta = {"source": name, "chunk": i, "from_file": "true"}
        if part["entry_date"]:
            meta["entry_date"] = part["entry_date"]
        new_entries.append((doc_id, part["text"], meta))
    # ADD FIRST, PRUNE LAST - the same order the upload path uses.
    #
    # The prune used to run here, above the batch. For a MODIFIED file the
    # stale set is the previous text of the chunks that changed, so deleting
    # it before the replacement embeds meant an embed timeout or a Chroma
    # error left the file with neither generation indexed. Content-addressed
    # ids make the reverse order safe: the worst case is both generations
    # present for the length of one batch, and the fingerprint below is only
    # written when everything landed, so the next boot re-runs the diff.
    #
    # The upload path was fixed for this earlier today and this one was not -
    # two replacement algorithms in one codebase, one of them safe. They agree
    # now, and test_replacement_durability covers both.
    added = add_documents_batch(new_entries, department=dept) if new_entries else 0
    if stale:
        delete_documents(sorted(stale), dept)
    log("kb_delta_ingest", file=name, chunks=len(parts), added=added,
        removed=len(stale), unchanged=len(desired) - added)
    # INVARIANT: the fingerprint is written ONLY after every chunk landed. A
    # failure mid-file leaves the source unfingerprinted, so the next boot
    # re-ingests it - and content-addressing makes that retry RESUME
    # (already-embedded chunks diff away) instead of starting over.
    state = _load_ingest_state()
    state[name] = _ingest_fingerprint(name, text)
    _save_ingest_state(state)
    return len(parts)


def _expected_chunk_count(name: str, text: str) -> int:
    """How many chunks this file SHOULD have in the index - same chunking and
    the same content-addressed id derivation as _ingest_file (dedup
    included), minus the embedding. Cheap (regex + md5), which is what lets
    the startup skip check verify COMPLETENESS, not just fingerprint
    equality: a source whose file has not changed passes the fingerprint +
    source-in-index check even when a wipe left it 3 chunks of 900 - and
    stays silently short FOREVER. A count mismatch re-ingests, and the
    content-addressed diff then embeds exactly what is missing."""
    dept = _dept_for_source(name)
    if dept == "history":
        parts = chunk_dated_markdown(text)
    else:
        parts = [{"text": c} for c in chunk_markdown_sections(text)]
    return len({hashlib.md5(f"{dept}::{name}::{p['text']}".encode(),
                            usedforsecurity=False).hexdigest() for p in parts})


def _sync_knowledge_dir(force: bool = True) -> dict:
    """Ingest / re-ingest every eligible file in KNOWLEDGE_DIR (recursive).
    force=False (startup) skips files whose ingest fingerprint is unchanged
    AND whose indexed chunk count matches the file's expected count - see
    _INGEST_STATE_PATH and _expected_chunk_count notes.

    Sources are keyed by path RELATIVE to KNOWLEDGE_DIR (posix form), not by
    basename: basename keying collides same-named files in different subdirs
    (three README.md's) into ONE source - their chunk doc_ids overwrite each
    other (silent data loss) and the ingest-state key can only remember one,
    so they re-embed every boot. Top-level files keep their old keys (rel ==
    name), so only subdir files re-embed once on the first deploy of this
    keying."""
    if not os.path.isdir(KNOWLEDGE_DIR):
        return {}
    results = {}
    state = _load_ingest_state()
    indexed = ({s["source"]: s["count"] for s in list_sources()}
               if not force else {})
    top_level = {f.name for f in pathlib.Path(KNOWLEDGE_DIR).iterdir() if f.is_file()}
    for p in pathlib.Path(KNOWLEDGE_DIR).rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _WATCHED_EXTS:
            continue
        rel = p.relative_to(KNOWLEDGE_DIR).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                continue
            # Self-healing migration (first boot does the work, later boots
            # no-op): purge the chunks + state entry this subdir file left
            # under its legacy BASENAME key - unless a top-level file
            # legitimately owns that name (then those chunks are ITS, not
            # stale copies).
            if rel != p.name and p.name not in top_level:
                delete_source(p.name, _dept_for_source(p.name))
                if state.pop(p.name, None) is not None:
                    _save_ingest_state(state)
            if (not force and state.get(rel) == _ingest_fingerprint(rel, text)
                    and indexed.get(rel) == _expected_chunk_count(rel, text)):
                results[rel] = {"status": "skipped"}
                continue
            n = _ingest_file(rel, text)
            results[rel] = {"status": "ok", "chunks": n}
        except Exception as e:
            results[rel] = {"status": "error", "error": str(e)}
    return results


def _sync_docs(force: bool = True) -> dict:
    """Ingest the configured root files and all files under docs/ into
    general RAG. force=False (startup) skips unchanged files - see
    _INGEST_STATE_PATH notes.

    Also prunes orphaned "docs/" sources whose file was deleted from disk -
    the index otherwise keeps their chunks forever, and a deleted doc would
    keep being retrieved indefinitely, crowding real answers out of the top
    results."""
    results = {}
    candidates = [f for f in _DOCS_ROOT_FILES if f.exists()]
    if _DOCS_DIR.is_dir():
        candidates += list(_DOCS_DIR.rglob("*"))
    archive_dir = _DOCS_DIR / "archive"
    synced: set[str] = set()
    state = _load_ingest_state()
    indexed = ({s["source"]: s["count"] for s in list_sources()}
               if not force else {})
    for p in candidates:
        if not p.is_file() or p.suffix.lower() not in _WATCHED_EXTS:
            continue
        # docs/archive/ = retired history. NEVER ingest it - it would
        # re-inject the exact stale content a doc reconciliation removed.
        # Excluded from _valid_doc_sources too, so already-ingested archive
        # chunks get orphan-pruned automatically.
        if archive_dir in p.parents:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                continue
            source = f"docs/{p.name}"
            synced.add(source)
            if (not force and state.get(source) == _ingest_fingerprint(source, text)
                    and indexed.get(source) == _expected_chunk_count(source, text)):
                results[source] = {"status": "skipped"}
                continue
            n = _ingest_file(source, text)
            results[source] = {"status": "ok", "chunks": n}
        except Exception as e:
            results[f"docs/{p.name}"] = {"status": "error", "error": str(e)}
    # Prune orphaned docs/ sources (deleted files whose chunks linger).
    try:
        for d in _prune_orphan_docs()["deleted"]:
            results[d["source"]] = {"status": "pruned", "chunks": d.get("count")}
    except Exception as e:
        # `except: pass` here meant the one job this block exists to do could
        # fail and still report a clean sync. The whole point of the prune is
        # removing documentation that was DELETED from disk but is still
        # retrievable - so a silent failure leaves the assistant answering from
        # files the operator believes are gone, and says nothing.
        results["docs/_prune"] = {"status": "error", "error": str(e)}
        log_error("docs_orphan_prune_failed", error=str(e))
    return results


def _valid_doc_sources() -> set[str]:
    """The 'docs/<name>' sources that SHOULD exist, derived from files on
    disk."""
    valid: set[str] = set()
    for f in _DOCS_ROOT_FILES:
        if f.exists():
            valid.add(f"docs/{f.name}")
    if _DOCS_DIR.is_dir():
        archive_dir = _DOCS_DIR / "archive"
        for p in _DOCS_DIR.rglob("*"):
            if archive_dir in p.parents:
                continue  # archived history is not a valid RAG source
            if p.is_file() and p.suffix.lower() in _WATCHED_EXTS:
                valid.add(f"docs/{p.name}")
    return valid


def _prune_orphan_docs() -> dict:
    """Delete 'docs/' sources whose backing file is gone - they otherwise
    linger in the index and pollute retrieval. Safe: the 'docs/' source
    namespace is exclusively file-derived (uploads and generated records never
    prefixed 'docs/'). Deletes from each source's ACTUAL department, and
    returns what it removed plus the remaining docs/ sources in the index -
    so the purge is observable, not silent."""
    valid = _valid_doc_sources()
    all_srcs = list_sources()
    deleted = []
    for s in all_srcs:
        name = str(s.get("source", ""))
        if name.startswith("docs/") and name not in valid:
            dept = s.get("department") or "general"
            delete_source(name, dept)
            deleted.append({"source": name, "department": dept, "count": s.get("count")})
    docs_in_index = sorted(
        ({"source": str(s.get("source")), "department": s.get("department"), "count": s.get("count")}
         for s in all_srcs if str(s.get("source", "")).startswith("docs/")),
        key=lambda x: x["source"] or "")
    return {"deleted": deleted, "docs_sources_in_index": docs_in_index}


def _handle_watched_change(deleted: bool, p: pathlib.Path) -> None:
    """One watcher event. Module-level so the delete-vs-replace distinction
    is testable without the async watchfiles machinery."""
    is_docs = str(p).startswith(str(_DOCS_DIR)) or p in _DOCS_ROOT_FILES
    # knowledge/ sources are keyed by KNOWLEDGE_DIR-relative path
    # (matches _sync_knowledge_dir - see its docstring for why)
    source = (f"docs/{p.name}" if is_docs
              else pathlib.Path(os.path.relpath(p, KNOWLEDGE_DIR)).as_posix())
    if deleted and not p.exists():
        dept = _dept_for_source(source)
        delete_source(source, dept)
        if dept != "general":
            delete_source(source, "general")  # legacy pre-routing chunks
        state = _load_ingest_state()
        if state.pop(source, None) is not None:
            _save_ingest_state(state)
        log("kb_file_deleted", file=source)
        return
    if deleted:
        # A "deleted" event for a file that still EXISTS is a REPLACE (git
        # checkout swaps files via unlink+rename). Taking it at face value -
        # delete_source wiping the whole source's metadata instantly, then a
        # container recreate racing the slow re-embed - is the whole
        # metadata-wipe family. Re-stat and do a normal delta re-ingest
        # instead: the content diff touches only chunks that actually
        # changed.
        text = p.read_text(encoding="utf-8", errors="ignore")
        if text.strip():
            n = _ingest_file(source, text)
            log("kb_file_replaced", file=source, chunks=n)
        return
    text = p.read_text(encoding="utf-8", errors="ignore")
    if text.strip():
        n = _ingest_file(source, text)
        log("kb_file_synced", file=source, chunks=n)


async def _watch_knowledge_dir():
    """Background task: re-ingest files whenever they change on disk."""
    try:
        from watchfiles import awatch, Change
    except ImportError:
        return
    watch_paths = [p for p in [KNOWLEDGE_DIR, str(_DOCS_DIR)] if os.path.isdir(p)]
    watch_paths += [str(f) for f in _DOCS_ROOT_FILES if f.exists()]
    if not watch_paths:
        return
    _archive_dir = _DOCS_DIR / "archive"
    async for changes in awatch(*watch_paths):
        for change_type, fpath in changes:
            p = pathlib.Path(fpath)
            if p.suffix.lower() not in _WATCHED_EXTS:
                continue
            # docs/archive/ = retired history; never live-ingest it (mirrors
            # the _sync_docs exclusion).
            if _archive_dir in p.parents:
                continue
            try:
                _handle_watched_change(change_type == Change.deleted, p)
            except Exception as e:
                log_error("kb_file_sync_error", file=str(p), error=str(e))
