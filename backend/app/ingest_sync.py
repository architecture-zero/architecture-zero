"""Knowledge-directory ingest: change detection, the file watcher, docs sync.

Not a router - no APIRouter, no routes. This is the subsystem the startup hooks
drive and the KB routes reuse, lifted out of main.py so both callers import it
from one place.

Direction is one-way: main -> ingest_sync, and main -> routers.kb -> ingest_sync.
Nothing here imports app.main.

_ingest_file_locked (the delta-ingest body; _ingest_file is the per-source
locking wrapper in front of it) and _sync_docs moved byte-for-byte and must
stay that way. Three tests read them through inspect.getsource and assert on
literal substrings and their ORDER - that add_documents_batch(new_entries
appears before delete_documents(sorted(stale), that the token add_document(
never appears, and that "docs_orphan_prune_failed" survives. Do not reflow
them, do not rename their locals, and do not run a formatter over this file.
"""
import os
import json
import asyncio
import hashlib
import logging
import pathlib
import threading
import contextlib

from app.chunking import (chunk_dated_markdown, chunk_markdown_sections,
                          CHUNKER_VERSION)
from app.database import (add_documents_batch, list_sources, delete_source,
                          get_source_ids, delete_documents, delete_file_chunks,
                          list_file_sources, restamp_reused_chunks)
from app.logger import log, log_error
from app.rag_config import dept_for_source as _dept_for_source
from app.text_extract import SUPPORTED_EXTS, TEXT_EXTS, extract_text

logger = logging.getLogger(__name__)


KNOWLEDGE_DIR                = os.path.abspath(os.getenv("KNOWLEDGE_DIR", "../knowledge"))
_DOCS_DIR                    = pathlib.Path(os.getenv("DOCS_DIR", "/app/docs"))
# Extra root-level files ingested alongside docs/ (comma-separated absolute
# paths) - a deploy that wants its PLAN/README in the corpus names them here.
_DOCS_ROOT_FILES             = [pathlib.Path(p.strip()) for p in
                                os.getenv("DOCS_ROOT_FILES", "").split(",") if p.strip()]
# Exactly the extractor's types (2026-10-29): this was its own literal, which
# watched .pdf without reading one as a PDF and did not watch .docx, a type the
# upload extracts. Set from SUPPORTED_EXTS so the two cannot drift; that adds
# .docx. Every content read goes through _read_watched below.
_WATCHED_EXTS                = {f".{ext}" for ext in SUPPORTED_EXTS}
# The session log is HISTORY, not current fact: a long-running log absorbs
# fact answers and crowds curated docs out of the default candidate pool.
# Sources mapped to history (rag_config.HISTORY_SOURCES) ingest into the
# kb_history department instead of general; the query router (app/routing.py,
# applied inside rerank.retrieve) pulls the history pool back in for
# history-shaped questions only. The source->department map lives in
# app/rag_config (dept_for_source) so retrieval AND the file-tool gate share
# ONE mapping; imported as _dept_for_source with the other imports above
# (a second, late import of the same name sat here until 2026-10-29).

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


def _read_watched(p: pathlib.Path, quiet: bool = False) -> str:
    """The text of one watched file: every content read of one in this module
    goes through here (2026-10-29). Each read was read_text(utf-8, errors
    ignored), so a PDF dropped into the knowledge tree or docs/ was indexed as
    its bytes decoded ("%PDF-1.4 ... endobj"), never its words.

    The text types are read exactly as before - read_text, which translates
    newlines - so an existing file's fingerprint and chunk ids do not move;
    extract_text decodes the bytes without that translation, so it is not
    used for them. pdf and docx go through text_extract.extract_text, as the
    upload's do. One it cannot read, or one with no text, comes back "" with
    one log line of bounded detail (none when quiet, for the corpus probe that
    runs every self-check pass): every caller skips blank text, so it is never
    indexed and never stops a sync, as the upload refuses one. An OSError
    reading the file propagates, as read_text's did."""
    if p.suffix.lower().lstrip(".") in TEXT_EXTS:
        return p.read_text(encoding="utf-8", errors="ignore")
    data = p.read_bytes()
    try:
        return extract_text(p.name, data)
    except Exception as e:
        if not quiet:
            log_error("kb_file_no_text", file=str(p), error=str(e)[:200])
        return ""




# -- Per-source ingest lock ----------------------------------------------------
# The watcher and the manual sync - or two manual syncs - can reach the SAME
# source at the same time, and a per-source ingest is not atomic: it purges the
# general-dept residue, diffs chunk ids, then writes. Two of those interleaving
# on one source can delete what the other just wrote, and concurrent writers on
# one collection are the shape that corrupts an HNSW index; this takes the
# same-source case of it off the table.
#
# PER-SOURCE, not global: different files are independent, and one global lock
# would serialise a whole sync behind its slowest file for no safety gain. The
# registry keeps one Lock per source name WITH WORK IN FLIGHT and nothing else
# (bounded 2026-10-29): until then it kept one per name it had ever seen, for
# the life of the process - every sync, upload and delete added one, a DELETE
# of a name never ingested included. A few bytes each, so a slow leak, never a
# wrong answer; but a process that stays up grows without bound. A hold counts
# itself in under the guard as it resolves its entry and the last hold out
# drops the entry; the count is what keeps a waiter from being handed a fresh
# Lock for a name whose holder just left.
#
# Every writer of a source takes that source's lock. In this module: the delta
# ingest through _ingest_file (the watcher, the startup and manual syncs), the
# watcher's delete, the docs and knowledge orphan prunes and the knowledge
# sync's legacy-key purge - the last three re-check the disk once they hold
# it. Outside it: the
# upload and the source-delete route (routers/kb.py), the quarantine release -
# which re-checks that its row is still held once it holds the lock - and the
# queued-ingest worker (app/jobs.py). /api/ingest writes one caller-named chunk
# in a single upsert, with no diff to interleave, and does not take it.
_ingest_locks: dict[str, threading.Lock] = {}
_ingest_lock_waiters: dict[str, int] = {}    # holds in flight per name: same keys, same guard
_ingest_locks_guard = threading.Lock()


# LOCK ORDER, fixed so nothing can deadlock: the upload's global
# _UPLOAD_WRITE_LOCK (routers/kb.py), the one other lock in play, is always
# taken BEFORE a source's lock and never while holding one; nothing holds two
# source locks at once; and no other path - the file syncs, the watcher, the
# prunes, the source delete, the quarantine release, the queued worker - takes
# the global lock at all. Every holder runs off the event loop: a threadpool
# route, the watcher's executor hop, the startup executor, the worker thread.
@contextlib.contextmanager
def _source_lock(source: str):
    """A hold on one source's lock, for `with`. NEVER acquire it on the event
    loop: a blocking acquire there, while a sync holds it, stalls every request
    for the length of that ingest - which is why the watcher hops to an
    executor first.

    A context manager, not the Lock (2026-10-29; it returned the table's Lock
    until then, which left the table no way to learn that a hold had ended, so
    an entry stayed for the life of the process): the entry is resolved and
    this hold counted in under one take of the guard, so no exit can drop the
    entry in between; the Lock is held across the body, the guard never across
    the acquire; and on the way out the last hold counted drops the entry.
    Every caller takes it as `with _source_lock(name):` - the one shape that
    pairs the count-in with the count-out.
    """
    with _ingest_locks_guard:
        lock = _ingest_locks.setdefault(source, threading.Lock())
        _ingest_lock_waiters[source] = _ingest_lock_waiters.get(source, 0) + 1
    try:
        with lock:
            yield lock
    finally:
        with _ingest_locks_guard:
            left = _ingest_lock_waiters[source] - 1
            if left:
                _ingest_lock_waiters[source] = left
            else:
                del _ingest_lock_waiters[source]
                del _ingest_locks[source]


def _ingest_file(name: str, text: str) -> int:
    """Serialised entry point - one writer per source at a time.

    A thin wrapper so the body below keeps its shape. Every file-sync add path
    funnels through here - the watcher, the startup syncs and the manual sync -
    which is what makes one lock here sufficient for them.
    """
    with _source_lock(name):
        return _ingest_file_locked(name, text)


def _ingest_file_locked(name: str, text: str) -> int:
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
    kept_entries = []
    for doc_id, (i, part) in desired.items():
        meta = {"source": name, "chunk": i, "from_file": "true"}
        if part["entry_date"]:
            meta["entry_date"] = part["entry_date"]
        if doc_id in existing:
            # unchanged chunk: already embedded, never re-embed it
            kept_entries.append((doc_id, part["text"], meta))
            continue
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
    # A kept id may be one an UPLOAD wrote (2026-10-29): the same id formula,
    # so a file whose text repeats an upload of its name lands on the upload's
    # rows, which kept the upload's metadata - no stamp, the uploader's tier -
    # and this file's source read as not file-owned. They become the file's:
    # the stamp and the curated tier, no re-embed. A row this sync already
    # stamped is read, not rewritten. Before the prune below, the add-first
    # order.
    if kept_entries:
        restamp_reused_chunks(kept_entries, department=dept)
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
            text = _read_watched(p)
            if not text.strip():
                continue
            # Self-healing migration (first boot does the work, later boots
            # no-op): purge the chunks + state entry this subdir file left
            # under its legacy BASENAME key - unless a top-level file
            # legitimately owns that name (then those chunks are ITS, not
            # stale copies). Under that key's lock, and re-checked there: a
            # top-level file of this name written since the listing above is
            # the watcher's to ingest, not this purge's to delete. Only the
            # chunks a file sync stamped (from_file): an upload is keyed by
            # its bare file name too, is no leftover of this sync, and
            # delete_source would take it on every sync, boot included.
            if rel != p.name and p.name not in top_level:
                with _source_lock(p.name):
                    if not (pathlib.Path(KNOWLEDGE_DIR) / p.name).is_file():
                        delete_file_chunks(p.name, _dept_for_source(p.name))
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
    # Prune knowledge sources whose file is gone. The watcher removes a deleted
    # file's chunks only while it runs, so a file deleted while the backend was
    # down kept serving with nothing left to remove it - the job _sync_docs's
    # prune does for docs/, and its failure is reported for the same reason.
    try:
        pr = _prune_orphan_knowledge()
        for d in pr["deleted"]:
            results[d["source"]] = {"status": "pruned", "chunks": d.get("count")}
        if pr.get("skipped"):
            results["knowledge/_prune"] = {"status": "skipped", "reason": pr["skipped"]}
    except Exception as e:
        results["knowledge/_prune"] = {"status": "error", "error": str(e)}
        log_error("knowledge_orphan_prune_failed", error=str(e))
    return results


def _valid_knowledge_sources(knowledge_dir: str | None = None) -> set[str]:
    """The KNOWLEDGE_DIR-relative keys that SHOULD exist: every eligible file
    on disk, keyed exactly as _sync_knowledge_dir keys it.

    The KB file listing (routers/kb.py kb_files) walks the tree through this
    helper too, since 2026-10-28, passing its own KNOWLEDGE_DIR binding (the
    one its tests patch): one walk, so the listing can neither show a file
    the sync would not ingest nor miss one it would - the root-only iterdir
    listing it replaced never showed a subfolder file at all."""
    root = pathlib.Path(KNOWLEDGE_DIR if knowledge_dir is None else knowledge_dir)
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in _WATCHED_EXTS}


# More candidates than this in one pass is a refusal, not a prune: a
# half-mounted or swapped knowledge volume reads as "every file was deleted",
# and this must never be the thing that empties the index. The operator lever
# (POST /api/admin/kb/prune-orphans?force=true) takes a larger set on purpose.
_KNOWLEDGE_PRUNE_CAP = 10


def _prune_orphan_knowledge(force: bool = False, dry_run: bool = False) -> dict:
    """Delete knowledge/ sources whose file is gone.

    The watcher removes a deleted file's chunks only while it is running, so a
    deletion that lands between processes - a pull during an update, a file
    removed while the container was stopped - left the chunks retrievable
    indefinitely.

    A candidate is a source the FILE SYNC wholly owns - every chunk stamped
    from_file="true" by _ingest_file_locked - whose key is not a file on disk
    and is not docs/ (that namespace is _prune_orphan_docs's). An upload is
    named by its file name, the same shape, but carries no from_file stamp,
    and neither do API ingests or generated records, so none of them can be a
    candidate; a source with mixed chunks is reported, not deleted. The help
    pages carry the stamp too, and list_file_sources never lists them. The
    candidate list is a snapshot: under each source's lock the file-on-disk
    check and the provenance read both run again, and a source no longer
    wholly file-owned by then is skipped with a log line, every chunk left.
    Fail-safe: a missing or empty knowledge dir prunes nothing, and more than
    _KNOWLEDGE_PRUNE_CAP candidates prunes nothing unless force. dry_run lists
    what it would remove and removes nothing."""
    root = pathlib.Path(KNOWLEDGE_DIR)
    if not root.is_dir():
        return {"deleted": [], "skipped": "knowledge dir missing"}
    valid = _valid_knowledge_sources()
    if not valid:
        return {"deleted": [], "skipped": "knowledge dir holds no eligible file"}
    orphans = [s for s in list_file_sources()
               if s["file_chunks"] and not s["source"].startswith("docs/")
               and s["source"] not in valid]
    candidates = [s for s in orphans if s["file_chunks"] == s["count"]]
    mixed = [{"source": s["source"], "department": s["department"],
              "count": s["count"], "file_chunks": s["file_chunks"]}
             for s in orphans if s["file_chunks"] != s["count"]]
    plan = [{"source": s["source"], "department": s["department"], "count": s["count"]}
            for s in candidates]
    if dry_run:
        return {"deleted": [], "would_delete": plan, "mixed": mixed}
    if len(candidates) > _KNOWLEDGE_PRUNE_CAP and not force:
        log_error("knowledge_orphan_prune_refused", candidates=len(candidates),
                  cap=_KNOWLEDGE_PRUNE_CAP)
        return {"deleted": [], "would_delete": plan, "mixed": mixed,
                "skipped": f"{len(candidates)} candidates exceed the cap of "
                           f"{_KNOWLEDGE_PRUNE_CAP}"}
    deleted = []
    for s in candidates:
        with _source_lock(s["source"]):
            # Re-checked under the lock: a sync or the watcher may have written
            # the file back since the listing, and deleting now would drop
            # what it wrote.
            if (root / s["source"]).is_file():
                continue
            # The provenance is re-read under the lock too (2026-10-29): the
            # candidate list was taken before any lock, and an upload named
            # like the deleted file takes this same lock, replaces the
            # file-owned chunks with its own unstamped ones and writes no
            # knowledge file - so a source that read as an orphan at the
            # listing is an upload by the time this lock is held, and the
            # disk check above cannot tell. Until this re-read the prune
            # deleted that upload whole. The listing is the one the snapshot
            # used, taken again now (one corpus listing per candidate, bounded
            # by the cap unless forced); only a source that still reads wholly
            # file-owned is deleted, with the count it holds now. A source
            # that reads mixed here is left whole, as the snapshot leaves a
            # mixed source - not trimmed with delete_file_chunks: the dry run
            # never offered a partial delete. (Until 2026-10-29 the file-owned
            # rows of a raced source could also be the upload's own text: a
            # chunk id the upload shared with the old file kept that file's
            # stamp. The upload re-stamps such ids now - restamp_reused_chunks
            # in app/database.py.)
            live = next((r for r in list_file_sources()
                         if r["source"] == s["source"]
                         and r["department"] == s["department"]), None)
            if live is None or live["file_chunks"] != live["count"]:
                log("kb_orphan_prune_skipped", file=s["source"],
                    department=s["department"],
                    count=live["count"] if live else 0,
                    file_chunks=live["file_chunks"] if live else 0,
                    reason="nothing indexed under the lock" if live is None
                           else "not wholly file-owned under the lock")
                continue
            delete_source(s["source"], s["department"])
            state = _load_ingest_state()
            if state.pop(s["source"], None) is not None:
                _save_ingest_state(state)
        log("kb_orphan_pruned", file=s["source"], department=s["department"],
            chunks=live["count"])
        deleted.append({"source": s["source"], "department": s["department"],
                        "count": live["count"]})
    return {"deleted": deleted, "mixed": mixed}


# The most entries one _knowledge_dir_has_documents call looks at.
_DOCUMENT_SCAN_LIMIT = 10_000


def _knowledge_dir_has_documents() -> bool:
    """Whether KNOWLEDGE_DIR holds a document _sync_knowledge_dir would ingest,
    by its rule: a file at any depth, a watched suffix, text that is not blank.
    The startup hook passes this to the retrieval-lane probe as
    corpus_expected (2026-10-08, filed by the R-AZ-01 read): an empty vector
    store beside documents waiting to be served is an error, not
    "not_required". An empty or missing directory still reads not_required.

    It runs on every self-check pass, so it stops at the first document and
    reads a file only until its first non-blank text. A tree with none is
    walked to the end, but never past _DOCUMENT_SCAN_LIMIT entries: a tree that
    big counts as holding documents, as does any error on the way - a
    directory that cannot be listed, a watched file that cannot be read, an
    exception from the walk itself: "not required" is found by a complete,
    clean look, never assumed. os.walk reports a listing error (pathlib's rglob
    swallowed it, and read a document in an unreadable directory as none - the
    2026-10-08 read); symlinked directories are not followed, as before.

    A pdf or docx is read as the sync reads it (2026-10-29), through
    _read_watched: one with no text is not a document. Parsing one is the
    expensive read, so they are set aside during the walk and parsed only if
    no text file answered - a tree holding any text document parses none."""
    if not os.path.isdir(KNOWLEDGE_DIR):
        return False
    failed: list = []
    seen = 0
    extracted: list[str] = []
    try:
        for root, dirs, files in os.walk(KNOWLEDGE_DIR, onerror=failed.append):
            if failed:
                return True
            seen += len(dirs)
            for name in files:
                seen += 1
                if seen > _DOCUMENT_SCAN_LIMIT:
                    return True
                path = os.path.join(root, name)
                if os.path.splitext(name)[1].lower() not in _WATCHED_EXTS or not os.path.isfile(path):
                    continue
                if os.path.splitext(name)[1].lower().lstrip(".") not in TEXT_EXTS:
                    extracted.append(path)
                    continue
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        piece = f.read(4096)
                        while piece:
                            if piece.strip():
                                return True
                            piece = f.read(4096)
                except OSError:
                    return True   # a watched file that cannot be read is still the operator's document
        for path in extracted:
            try:
                if _read_watched(pathlib.Path(path), quiet=True).strip():
                    return True
            except OSError:
                return True
    except Exception:
        return True
    return bool(failed)


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
            text = _read_watched(p)
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


def _prune_orphan_docs(dry_run: bool = False) -> dict:
    """Delete 'docs/' sources whose backing file is gone - they otherwise
    linger in the index and pollute retrieval. Safe: the 'docs/' source
    namespace is exclusively file-derived (uploads and generated records never
    prefixed 'docs/'). Deletes from each source's ACTUAL department, and
    returns what it removed plus the remaining docs/ sources in the index -
    so the purge is observable, not silent. dry_run lists what it would
    remove and removes nothing."""
    valid = _valid_doc_sources()
    all_srcs = list_sources()
    deleted = []
    for s in all_srcs:
        name = str(s.get("source", ""))
        if name.startswith("docs/") and name not in valid:
            dept = s.get("department") or "general"
            if not dry_run:
                with _source_lock(name):
                    # Re-checked under the lock: a file of this name written
                    # back since the listing above is the watcher's to ingest,
                    # and deleting now would drop what it wrote.
                    if name in _valid_doc_sources():
                        continue
                    delete_source(name, dept)
            deleted.append({"source": name, "department": dept, "count": s.get("count")})
    docs_in_index = sorted(
        ({"source": str(s.get("source")), "department": s.get("department"), "count": s.get("count")}
         for s in all_srcs if str(s.get("source", "")).startswith("docs/")),
        key=lambda x: x["source"] or "")
    if dry_run:
        return {"deleted": [], "would_delete": deleted,
                "docs_sources_in_index": docs_in_index}
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
        # Same lock as the add path: a delete racing a sync's re-ingest of
        # this source is the interleave the lock exists for.
        with _source_lock(source):
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
        text = _read_watched(p)
        if text.strip():
            n = _ingest_file(source, text)
            log("kb_file_replaced", file=source, chunks=n)
        return
    text = _read_watched(p)
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
                # OFF THE EVENT LOOP. An inline call here was already a latency
                # bug - a slow re-embed froze every other request on the
                # process - and the per-source lock makes it a correctness
                # requirement: a blocking acquire on the loop, while a sync
                # holds the lock, would stall the whole backend for the length
                # of that ingest. Still awaited, so a batch's changes are
                # handled one at a time and in order, as before.
                await asyncio.get_running_loop().run_in_executor(
                    None, _handle_watched_change, change_type == Change.deleted, p)
            except Exception as e:
                log_error("kb_file_sync_error", file=str(p), error=str(e))
