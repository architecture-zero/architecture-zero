"""Boot-path index maintenance: heal vector/metadata divergence, purge orphaned
segments, report index parameter drift.

WHY. Chroma 0.5.x flushes a collection's HNSW binary only every
`hnsw:sync_threshold` consumed records and never on close, while the write-ahead
log purges as soon as consumption is recorded. An unclean stop therefore
permanently loses every vector written since the last flush WHILE THE SQLITE
METADATA SURVIVES - and a collection living under the threshold never flushes at
all. The surviving metadata then makes delta ingestion SKIP re-embedding, so the
corpus quietly serves records whose vectors are gone. That failure is silent by
construction: the ingest skip-check counts records PRESENT in metadata, and a
dead record is still present.

WHAT RUNS EVERY BOOT, before the ingest syncs, embed-free:

1. purge_orphan_segments - stranded sqlite rows and on-disk dirs left behind by
   delete_collection, which removes the collection and its segments rows but not
   the embeddings / embedding_metadata / max_seq_id rows or the vector directory
   keyed by the now-gone segment. The sweep runs before AND after step 2,
   because a rebuild in step 2 creates exactly this debris itself.
2. Per collection: export ids/documents/metadatas (the sqlite-backed truth) and,
   separately, embeddings. A record whose embedding cannot be read is DEAD - its
   metadata outlived its vector. Dead records are dropped and the ingest
   fingerprints of every source that lost chunks are cleared, so the syncs
   below re-embed exactly the missing chunks. File-backed gaps self-heal;
   anything not backed by a file on disk is logged LOUDLY by name rather than
   vanishing quietly.

WHAT DOES NOT RUN BY ITSELF. Rebuilding a collection - dropping it and re-adding
the exported records - is the only operation here that destroys a currently
HEALTHY collection, and its export lives in memory alone until the re-add
finishes. It fires on exactly two triggers, never on a parameter difference:

  - a HALTED export, meaning the index errored mid-read. The collection is
    already broken, a rebuild cannot make it worse, and it is the only cure.
  - the force-rebuild flag, a deliberate operator gesture.

Index parameter drift is REPORTED and never acted on. Every collection here is
created with the same parameter dict, so automatic adoption would carry the
entire blast radius for almost no benefit; the check earns its place as a
detector, because changing a parameter is otherwise a silent no-op.

The rebuild re-adds records that already passed the ingestion gate, with their
metadata - trust tier, injection flags - preserved verbatim. It is a restore of
gated content, not a new ingestion surface.

Runs inside the startup executor thread and must never raise out: a collection
whose maintenance fails is reported and left as it was. Fail-open like the rest
of the retrieval stack, but never fail-silent.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
from typing import Callable, Iterable

from app.database import (
    client as _default_client,
    collection_metadata,
    _invalidate_lexical_index,
    _CHROMA_PATH,
)

log = logging.getLogger("chroma_maintenance")

_PAGE = 500

# One-shot operator lever: drop `force-rebuild.json` (a JSON list of collection
# names) into the data directory and the NEXT boot forces those collections down
# the rebuild path, then deletes the file so it fires exactly once.
#
# It exists because WRITE-side index corruption has no safe in-process probe: an
# index can segfault natively inside the vector library on the first write of
# every boot while every read-side check stays green - export clean, knn probe
# ok, zero dead records - because a write canary would simply BE the crash. The
# rebuild path never touches the sick graph, so it is what cures this class.
# A boot that dies mid-rebuild still consumes the flag; drop the file again.
_FORCE_REBUILD_FILE = "force-rebuild.json"


def _consume_force_rebuild(chroma_path: str | None = None) -> set:
    path = os.path.join(os.path.abspath(chroma_path or _CHROMA_PATH),
                        _FORCE_REBUILD_FILE)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            names = json.load(f)
        if not (isinstance(names, list)
                and all(isinstance(n, str) for n in names)):
            raise ValueError(f"expected a JSON list of names, got: {names!r}")
        log.warning("force-rebuild flag consumed: %s", names)
        return set(names)
    except Exception as e:
        log.error("force-rebuild flag unreadable, ignoring it: %s", e)
        return set()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _export_records(col) -> tuple[list, list, list]:
    """Page out ids/documents/metadatas - the metadata-segment truth, which
    survives the crashes that kill vectors."""
    ids, docs, metas = [], [], []
    offset = 0
    while True:
        got = col.get(limit=_PAGE, offset=offset, include=["documents", "metadatas"])
        page_ids = got.get("ids") or []
        if not page_ids:
            break
        page_docs = got.get("documents")
        page_metas = got.get("metadatas")
        ids.extend(page_ids)
        docs.extend(page_docs if page_docs is not None else [None] * len(page_ids))
        metas.extend(page_metas if page_metas is not None else [None] * len(page_ids))
        offset += len(page_ids)
        if len(page_ids) < _PAGE:
            break
    return ids, docs, metas


def _export_embeddings(col) -> tuple[dict, bool]:
    """(id -> embedding, halted) for every vector the index can still read.

    A dead index raises or returns partial results; both mean "those records
    have no vector", which is exactly what the caller partitions on - so a
    failure here is DATA, not an error.

    But HOW it failed matters downstream. `halted=True` means the index itself
    errored mid-read: it is CORRUPT rather than merely incomplete, and the
    caller must never run an in-place delete against it. Deleting from a corrupt
    index can crash the process inside the native vector library - no traceback,
    no Python exception to catch - and a supervisor then restarts the container
    straight back into the same crash, a loop no unattended boot escapes.

    Numpy-safe by construction: chroma returns embeddings as arrays, and
    truthy-testing an array raises, so this uses only `is None` checks and
    explicit float conversion.
    """
    out: dict = {}
    offset = 0
    halted = False
    try:
        while True:
            got = col.get(limit=_PAGE, offset=offset, include=["embeddings"])
            page_ids = got.get("ids") or []
            if not page_ids:
                break
            embs = got.get("embeddings")
            for i, rid in enumerate(page_ids):
                if embs is None or i >= len(embs):
                    continue
                e = embs[i]
                if e is None:
                    continue
                try:
                    vec = [float(x) for x in e]
                except (TypeError, ValueError):
                    continue
                if vec:
                    out[rid] = vec
            offset += len(page_ids)
            if len(page_ids) < _PAGE:
                break
    except Exception as e:
        halted = True
        log.warning("embedding export halted for %s at offset %d "
                    "(records past this point count as dead; the index is "
                    "treated as CORRUPT and the rebuild path is forced): %s",
                    getattr(col, "name", "?"), offset, e)
    return out, halted


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def purge_orphan_segments(chroma_path: str | None = None) -> dict:
    """Remove the debris delete_collection leaves behind.

    delete_collection removes the collection and its segments rows but STRANDS
    the metadata-segment content - embeddings rows with their
    embedding_metadata, max_seq_id rows, and the vector segment's on-disk
    directory, all keyed by the now-gone segment id. The sweep targets two
    levels: segment rows whose collection is gone, then child rows and dirs
    whose segment is gone.

    embeddings_queue is deliberately left alone - it is the live replay source
    and the library keeps it clear of dead topics itself. FTS residue in
    embedding_fulltext_search is left alone too: contentless-table deletes need
    special handling and nothing in the query path reads it.

    Uses its own sqlite connection beside the live client. What makes that safe
    is NOT journal mode - this store runs in rollback-journal mode, where a
    writer takes an exclusive lock - but that the library's own connections are
    short-lived per operation, and the busy timeout below degrades a collision
    into a wait rather than an error.
    """
    path = os.path.abspath(chroma_path or _CHROMA_PATH)
    db = os.path.join(path, "chroma.sqlite3")
    out = {"segments": 0, "embedding_rows": 0, "dirs": 0}
    if not os.path.exists(db):
        return out
    con = sqlite3.connect(db, timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        # Level 1: segment rows whose collection no longer exists.
        out["segments"] = con.execute(
            "SELECT count(*) FROM segments "
            "WHERE collection NOT IN (SELECT id FROM collections)").fetchone()[0]
        con.execute("DELETE FROM segments "
                    "WHERE collection NOT IN (SELECT id FROM collections)")
        # Level 2: child rows whose segment row is gone (including what level 1
        # just removed - the deletes cascade through the same predicate).
        out["embedding_rows"] = con.execute(
            "SELECT count(*) FROM embeddings "
            "WHERE segment_id NOT IN (SELECT id FROM segments)").fetchone()[0]
        con.execute("DELETE FROM embedding_metadata WHERE id IN "
                    "(SELECT id FROM embeddings "
                    " WHERE segment_id NOT IN (SELECT id FROM segments))")
        con.execute("DELETE FROM embeddings "
                    "WHERE segment_id NOT IN (SELECT id FROM segments)")
        con.execute("DELETE FROM max_seq_id "
                    "WHERE segment_id NOT IN (SELECT id FROM segments)")
        con.execute("DELETE FROM segment_metadata "
                    "WHERE segment_id NOT IN (SELECT id FROM segments)")
        con.commit()
        live_segs = {str(r[0]) for r in con.execute("SELECT id FROM segments")}
    finally:
        con.close()
    # On-disk vector dirs whose segment is gone. THREE conjoined conditions, and
    # all three matter: this directory is the operator's data directory - it
    # also holds the relational database, the backup folder and the ingest state
    # - so the sweep touches a name only when it is a directory, its name is
    # exactly a UUID (fullmatch, never search), and no live segment claims it.
    # The dir name IS the vector segment's id; that identity is what makes the
    # last condition meaningful rather than accidental, and it is pinned by a
    # test, because a future version keying dirs differently would turn this
    # loop into one that deletes every LIVE index instead.
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        if (os.path.isdir(full) and _UUID_RE.fullmatch(entry)
                and entry not in live_segs):
            shutil.rmtree(full, ignore_errors=True)
            out["dirs"] += 1
    if out["segments"] or out["embedding_rows"] or out["dirs"]:
        log.warning("purged orphan debris: %d segment row(s), %d stranded "
                    "embedding row(s), %d on-disk dir(s)",
                    out["segments"], out["embedding_rows"], out["dirs"])
    return out


def _maintain_collection(name: str, target: dict,
                         clear_fingerprints: Callable[[Iterable[str]], None],
                         client, force_rebuild: bool = False) -> dict:
    # Inspect via get_collection: get_or_create_collection ignores a new
    # metadata dict for an existing collection, but relying on that quirk would
    # make this check self-blinding if it ever changed.
    col = client.get_collection(name=name)
    meta = col.metadata or {}
    # hnsw:space is excluded deliberately: including it would drag every
    # collection created before that key existed through a rebuild.
    params_ok = all(meta.get(k) == v for k, v in target.items()
                    if k != "hnsw:space")

    ids, docs, metas = _export_records(col)
    embs, halted = _export_embeddings(col)
    healthy = [i for i, rid in enumerate(ids) if rid in embs]
    dead = [i for i, rid in enumerate(ids) if rid not in embs]
    res = {"status": "ok", "params_ok": params_ok, "records": len(ids),
           "dead": len(dead), "rebuilt": False, "halted": halted}
    if force_rebuild:
        res["forced"] = True
    if not params_ok:
        # REPORTED, never acted on. Adopting new parameters means dropping and
        # re-adding a healthy collection, and the export exists only in memory
        # until the re-add completes - too much blast radius for a difference
        # that, on an instance where every collection is created with the same
        # dict, should not arise in the first place. If it has arisen, an
        # operator wants to know why before anything is rebuilt.
        res["params_drift"] = True
        log.error("%s: index parameters differ from the target - live %s, "
                  "target %s. NOT rebuilding automatically; to adopt them, put "
                  "this collection in %s and restart once.",
                  name, {k: meta.get(k) for k in target if k != "hnsw:space"},
                  {k: v for k, v in target.items() if k != "hnsw:space"},
                  _FORCE_REBUILD_FILE)

    if not dead and not halted and not force_rebuild:
        return res

    dead_sources = sorted({(metas[i] or {}).get("source", "unknown") for i in dead})
    non_file = sorted({(metas[i] or {}).get("source", "unknown") for i in dead
                       if (metas[i] or {}).get("from_file") != "true"})

    # A halted export means the index errored mid-read: it is CORRUPT, and the
    # in-place delete below would run against that same corrupt index - which
    # can crash the process natively and boot-loop the container, a state no
    # unattended boot recovers from. The rebuild path never touches the corrupt
    # index; it drops the collection and re-adds the healthy exports. So a halt
    # FORCES the rebuild regardless of anything else.
    if halted:
        log.error("%s: embedding export HALTED - forcing the rebuild path "
                  "(an in-place delete against a corrupt index risks a native "
                  "crash and a restart loop)", name)
    if force_rebuild:
        log.warning("%s: rebuild FORCED by the force-rebuild flag "
                    "(write-side corruption lever - see %s)",
                    name, _FORCE_REBUILD_FILE)
    if halted or force_rebuild:
        client.delete_collection(name)
        # get_or_create + upsert, NOT create + add. Routers are serving while
        # this runs, and any read path can re-mint a collection through
        # get_or_create_collection between the delete above and this line - in
        # which case create_collection raises a uniqueness error, the export is
        # dropped on the floor, and the collection keeps only what the racer
        # wrote. Adopting the racer's collection is correct here rather than
        # lossy, because every re-minter stamps this same parameter dict.
        new_col = client.get_or_create_collection(name=name,
                                                  metadata=dict(target))
        for start in range(0, len(healthy), _PAGE):
            batch = healthy[start:start + _PAGE]
            new_col.upsert(
                ids=[ids[i] for i in batch],
                embeddings=[embs[ids[i]] for i in batch],
                documents=[docs[i] for i in batch],
                metadatas=[(metas[i] if metas[i] else {"source": "unknown"})
                           for i in batch],
            )
        res["rebuilt"] = True
        col = new_col
        count = col.count()
        if count < len(healthy):
            # Belt and braces: force a file re-ingest of everything this
            # collection held; content addressing keeps the re-embed minimal.
            res["status"] = "count_mismatch"
            log.error("rebuild of %s holds %d of %d healthy records - "
                      "clearing ALL its source fingerprints for file re-ingest",
                      name, count, len(healthy))
            clear_fingerprints(sorted({(m or {}).get("source", "unknown")
                                       for m in metas}))
    elif dead:
        # Parameters are not the issue and the index reads cleanly: just drop
        # the dead records so the fingerprint clear below makes the startup
        # sync re-embed exactly those chunks.
        col.delete(ids=[ids[i] for i in dead])

    if dead:
        clear_fingerprints(dead_sources)
        log.warning("%s: dropped %d dead record(s) (metadata outlived vector) "
                    "across source(s) %s - file-backed chunks re-embed on this "
                    "boot's sync", name, len(dead), dead_sources)
        if non_file:
            log.error("%s: %d dead record(s) belong to source(s) %s that are "
                      "NOT backed by a file on disk. Generated records are "
                      "rewritten later in this same boot; uploaded documents "
                      "have no other copy and are gone.",
                      name, len(dead), non_file)
            res["lost_non_file_sources"] = non_file

    _invalidate_lexical_index(name)

    # knn probe: the point of an index is retrievability, and visibility is not
    # retrievability - a record can export cleanly and still not come back from
    # a search. Prove it with a record's own vector.
    if healthy:
        try:
            col.query(query_embeddings=[embs[ids[healthy[0]]]], n_results=1)
        except Exception as e:
            res["status"] = "probe_failed"
            log.error("post-maintenance knn probe FAILED for %s: %s", name, e)
    return res


def _drop_stale_segment_instances(client, chroma_path: str | None = None) -> int:
    """Evict manager-cached instances of segments that no longer exist.

    delete_collection leaves the deleted collection's segment INSTANCES in the
    segment manager's cache; once the orphan sweep removes their directories,
    every later segment flush hits them and fails loudly. They die with the
    process anyway - this keeps THIS boot's own shutdown flush clean, which is
    the shutdown that matters most, because it is the one that persists the
    vectors written since startup.
    """
    path = os.path.abspath(chroma_path or _CHROMA_PATH)
    db = os.path.join(path, "chroma.sqlite3")
    if not os.path.exists(db):
        # No store means no live segment ids to compare against, and an empty
        # live set would evict EVERY cached instance. An absent database must
        # disable this, never widen it.
        return 0
    con = sqlite3.connect(db, timeout=30)
    try:
        live = {str(r[0]) for r in con.execute("SELECT id FROM segments")}
    finally:
        con.close()
    manager = client._server._manager
    dropped = 0
    for seg_id in list(manager._instances.keys()):
        if str(seg_id) not in live:
            inst = manager._instances.pop(seg_id)
            try:
                inst.stop()
            except Exception:
                pass
            dropped += 1
    if dropped:
        log.warning("evicted %d stale segment instance(s) left by "
                    "delete_collection", dropped)
    return dropped


def run_chroma_maintenance(
        clear_fingerprints: Callable[[Iterable[str]], None],
        client=None, chroma_path: str | None = None) -> dict:
    """Entry point, called from the startup task BEFORE the ingest syncs - it
    clears ingest fingerprints those syncs then act on. Never raises."""
    client = client or _default_client
    target = collection_metadata()
    summary: dict = {"orphans_before": {}, "collections": {}, "orphans_after": {}}
    try:
        summary["orphans_before"] = purge_orphan_segments(chroma_path)
    except Exception as e:
        summary["orphans_before"] = {"error": str(e)}
        log.error("orphan segment purge failed: %s", e)
    try:
        names = [c.name for c in client.list_collections()]
    except Exception as e:
        summary["error"] = f"list_collections failed: {e}"
        log.error("chroma maintenance could not list collections: %s", e)
        return summary
    # Consume the flag only once the collection list is in hand. Consuming it
    # earlier means a failure to list deletes the operator's request without
    # rebuilding anything and without naming what it wanted rebuilt.
    forced = _consume_force_rebuild(chroma_path)
    unknown = forced - set(names)
    if unknown:
        # An operator typo must not vanish silently - the flag is already
        # consumed, so a misspelled name means the sick collection stays sick.
        log.error("force-rebuild flag names unknown collection(s) %s - "
                  "known: %s", sorted(unknown), sorted(names))
    for name in names:
        try:
            summary["collections"][name] = _maintain_collection(
                name, target, clear_fingerprints, client,
                force_rebuild=name in forced)
        except Exception as e:
            summary["collections"][name] = {"status": "error", "error": str(e)}
            log.error("chroma maintenance FAILED for %s: %s", name, e)
    # Second sweep: a rebuild above deletes a collection, and delete_collection
    # strands its metadata rows and vector directory - collect that debris in
    # the same boot that made it.
    try:
        summary["orphans_after"] = purge_orphan_segments(chroma_path)
    except Exception as e:
        summary["orphans_after"] = {"error": str(e)}
        log.error("post-rebuild orphan purge failed: %s", e)
    # ...and evict the cached segment instances the same behaviour leaves
    # behind, or this boot's own shutdown flush fails against purged dirs.
    try:
        summary["stale_instances"] = _drop_stale_segment_instances(
            client, chroma_path)
    except Exception as e:
        summary["stale_instances"] = {"error": str(e)}
        log.error("stale instance eviction failed: %s", e)
    return summary
