import hashlib
import json
import os
import logging
import math
import re
import time
from datetime import date, datetime, timezone

import requests
import chromadb

from app.rag_config import (
    EMBED_MODEL, VECTOR_WEIGHT, BM25_WEIGHT, BM25_K1, BM25_B, BM25_FETCH,
    RECENCY_HALF_LIFE_DAYS, RECENCY_FLOOR,
    HNSW_SYNC_THRESHOLD, HNSW_BATCH_SIZE, RERANK_FETCH,
    derive_trust,
)

log = logging.getLogger("database")

# chromadb 0.5.x fires a posthog telemetry attempt on EVERY collection op
# despite anonymized_telemetry=False, and each one logs an ERROR (capture()
# signature mismatch) - hundreds of junk lines/hour that bury real signals
# during incident forensics. Nothing is sent either way; silence the logger.
logging.getLogger("chromadb.telemetry").setLevel(logging.CRITICAL)

OLLAMA_BASE = os.getenv("OLLAMA_BASE", "http://localhost:11434")
# Embeddings always use a stable local endpoint - decoupled from chat
# inference. EMBED_BASE defaults to the host's local Ollama so RAG works even
# when OLLAMA_BASE points at a remote/tunneled GPU box that may be down.
EMBED_BASE  = os.getenv("EMBED_BASE", "http://host.docker.internal:11434")

_CHROMA_PATH = os.getenv("CHROMA_PATH", os.path.join(os.path.dirname(__file__), "..", "data"))
client = chromadb.PersistentClient(
    path=os.path.abspath(_CHROMA_PATH),
    settings=chromadb.Settings(anonymized_telemetry=False),
)

GLOBAL_COLLECTION = "knowledge_base"

# THE HELP COLLECTION (in-product help, ported 2026-09-21): the product's own
# help pages, synced from the image at boot (app/help_docs.py). It uses the
# department machinery (add/search/delete by department name) but it is NOT a
# department: it never appears in the department list, the residue report,
# the source listing, the document count, the corpus fingerprint, or the
# injection and PII source lists. _is_corpus_collection is the ONE predicate
# every corpus-wide enumeration uses, so a new enumeration inherits the
# exclusion the way a new ingestion path inherits the injection gate.
HELP_DEPARTMENT = "help"
HELP_COLLECTION = "kb_help"   # == _collection_name(HELP_DEPARTMENT), pinned by a test

# A kb_general minted before 2026-10-26, when a department typed "General"
# got a collection of its own (_collection_name says why that ended). Every
# listing labels a kb_* collection by its suffix, so this one read "general" -
# the global collection's label - and a delete aimed at it from a listing hit
# the global collection instead. Nothing writes or reads it now; it is kept
# out of every enumeration and named at boot (legacy_general_count). Report,
# don't destroy: its content was readable only by that department's members
# and the Owner, so merging it into the collection everyone reads would widen
# who sees it.
LEGACY_GENERAL_COLLECTION = "kb_general"


def _is_corpus_collection(name: str) -> bool:
    """A kb_* DEPARTMENT collection - the global, help and legacy kb_general
    collections are not."""
    return name.startswith("kb_") and name not in (
        GLOBAL_COLLECTION, HELP_COLLECTION, LEGACY_GENERAL_COLLECTION)


def collection_metadata() -> dict:
    """The ONE metadata dict every collection is created with. The hnsw
    persistence params are only honored at creation time in chroma 0.5.x -
    an existing collection must be rebuilt (export -> drop -> re-add) to
    adopt them (see rag_config for the why). Returns a fresh dict each call:
    chroma may hold a reference."""
    return {
        "hnsw:space": "cosine",
        "hnsw:batch_size": HNSW_BATCH_SIZE,
        "hnsw:sync_threshold": HNSW_SYNC_THRESHOLD,
    }


def flush_vector_segments() -> dict:
    """Force-persist every loaded HNSW vector segment to disk.

    chroma 0.5.23 has NO flush-on-close: PersistentLocalHnswSegment.stop()
    only closes file handles, so the sync_threshold persist inside
    _apply_batch is the only one that ever runs. Called from the FastAPI
    shutdown hook so a graceful stop (SIGTERM -> uvicorn drain, inside
    compose's stop_grace_period) writes the un-flushed tail instead of losing
    it.

    ORDER MATTERS: records below batch_size sit in the in-memory brute-force
    buffer, NOT in the hnsw index - persisting directly from there would
    record a max_seq_id covering vectors the saved index does not contain,
    turning a graceful stop into the exact loss this exists to prevent. So
    this mirrors the write path's own batch boundary verbatim (apply batch ->
    fresh Batch -> clear brute force -> persist). Private chroma internals by
    necessity - chromadb is PINNED at 0.5.23 in requirements.txt, and tests
    should pin every attribute used here - and every step degrades to the
    pre-fix behavior instead of breaking shutdown."""
    out = {"flushed": 0, "clean": 0, "errors": 0}
    try:
        instances = list(client._server._manager._instances.values())
    except Exception as e:
        log.warning("flush_vector_segments: cannot reach segment manager: %s", e)
        out["errors"] += 1
        return out
    for seg in instances:
        try:
            pending = getattr(seg, "_num_log_records_since_last_persist", None)
            if pending is None:
                continue  # metadata segment (or a future non-hnsw impl) - nothing to flush
            if not pending:
                out["clean"] += 1
                continue
            from chromadb.segment.impl.vector.batch import Batch
            from chromadb.utils.read_write_lock import WriteRWLock
            with WriteRWLock(seg._lock):
                batch = getattr(seg, "_curr_batch", None)
                if batch is not None and len(batch) > 0:
                    seg._apply_batch(batch)
                    seg._curr_batch = Batch()
                    bf = getattr(seg, "_brute_force_index", None)
                    if bf is not None:
                        bf.clear()
                if (seg._num_log_records_since_last_persist > 0
                        and getattr(seg, "_index", None) is not None):
                    seg._persist()
            out["flushed"] += 1
        except Exception as e:
            out["errors"] += 1
            log.warning("flush_vector_segments: segment flush failed: %s", e)
    return out


def _get_collection(department: str | None = None) -> chromadb.Collection:
    """Return the ChromaDB collection for the given department.

    None and every spelling of "general" -> global knowledge_base collection.
    Any other value  -> kb_<department> collection (created on first use).
    """
    return client.get_or_create_collection(
        name=_collection_name(department), metadata=collection_metadata())


def _collection_name(department: str | None = None) -> str:
    """The collection name for a department - ONE derivation, shared by the
    get-or-create path and the does-it-exist path so they cannot disagree.

    The SANITIZED name decides the global collection, not the spelling: until
    2026-10-26 only the exact "general" mapped there, so "General" minted
    kb_general, whose listing label is "general" (LEGACY_GENERAL_COLLECTION
    has the rest)."""
    if not department:
        return GLOBAL_COLLECTION
    # Sanitize: lowercase, replace spaces/special chars with underscore
    safe = re.sub(r"[^a-z0-9_-]", "_", department.lower())
    if safe == "general":
        return GLOBAL_COLLECTION
    return f"kb_{safe}"


def _existing_collection(department: str | None = None):
    """The department's collection, or None if it does not exist yet.

    The delete-side counterpart to _get_collection, which is get_or_create and
    therefore MINTS an empty collection as a side effect of being asked about
    one. That side effect is how ORDINARY code invents departments, not just
    probes: any maintenance path that calls delete_source when it has nothing
    to write ("if not rows: delete_source('metrics-current', 'metrics')")
    mints an empty collection on every run, which list_departments() then
    advertises as a real department.

    Found by the department-list invariant's boot report on a derived
    instance, whose residue turned out to come from exactly such ordinary
    paths - none of it probe residue. Without this helper, that report would
    fire at ERROR on every boot for a benign condition, and a guard that
    cries wolf every boot is one nobody reads.

    Deleting from a collection that does not exist is a no-op, so declining to
    create one is strictly better than creating it to find nothing inside.
    """
    name = _collection_name(department)
    # DELIBERATELY NOT wrapped in try/except. "chroma cannot answer" is not
    # "this department does not exist", and collapsing the two would fail
    # OPEN on the delete path: DELETE /api/ingest/source would report success
    # and unlink the on-disk source while every chunk stayed indexed and
    # retrievable. Letting the error propagate is also exactly what happened
    # before this helper existed - _get_collection would have raised too - so
    # callers' error handling is unchanged.
    if not any(c.name == name for c in client.list_collections()):
        return None
    # Existence settled, hand off to the ONE place collection objects are
    # produced. Going straight to client.get_collection here would fork that
    # seam - and _get_collection is what the suite patches to stub collection
    # access, so a second path would quietly drop those stubs (the BM25 suite
    # caught exactly that). get_or_create on a collection already
    # proven to exist creates nothing.
    return _get_collection(department)


def _embed(text: str, retries: int = 2, timeout: float = 60) -> list[float]:
    """Embed with retry + backoff. One transient slow/failed embedding call
    must not abort a whole-file ingest: a big file rolls the dice hundreds of
    times, so big files die first without the retry. Timeout is per-attempt
    and sized for a CPU-contended box, not the idle-case latency (the lane
    probe below passes a shorter one: it asks one short sentence)."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                f"{EMBED_BASE}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=timeout
            )
            response.raise_for_status()
            return response.json()["embedding"]
        except Exception as e:
            last_err = e
            if attempt < retries:
                log.warning("embed attempt %d/%d failed, retrying: %s",
                            attempt + 1, retries + 1, e)
                time.sleep(2 * (attempt + 1))
    raise last_err


# -- The retrieval lane, proven (AZ-02) ---------------------------------------
_LANE_PROBE_TEXT = "Is the retrieval lane answering?"

# The collections the lane probe last found refusing its search - logged when
# that set changes, not every pass (the self-check's own rule for its log).
_lane_failed_last: tuple = ()


def probe_retrieval_lane(rag_only: bool, corpus_expected: bool = False) -> dict:
    """One pass over what a governed query needs (AZ-02, outside review
    2026-10-06; fixed 2026-10-07). Readiness proved the database and nothing
    else, while retrieval gets its embeddings from EMBED_BASE - a separate
    service that a healthy database, a healthy cloud chat provider or a healthy
    OLLAMA_BASE says nothing about.

    In order: the vector store answers a read; the embed service answers with
    a vector through the SAME _embed a query uses; every populated collection
    accepts that vector for a search. So a missing EMBED_MODEL fails here as it
    fails every query (the service refuses it), and so does a different model
    (it answers with a vector of the wrong width, which the search rejects).

    Each collection is searched the way a question searches it (2026-10-09):
    through _knn, at the k query_similar asks for when the chat lane retrieves
    (RERANK_FETCH candidates). The probe used to search the first populated
    collection only, at k=1, so a derived instance read ready for hours while
    its main collection refused the k every question asked (hnswlib, near the
    index's size) - the probe never asked it that - and a sick collection
    that is not first in the list was never searched at all. The probe now
    proves what a question runs, and a collection that fails names itself in
    the log.

    Returns {"state": "ok"}, {"state": "not_required"} when this instance
    serves no retrieval (no operator documents, RAG_ONLY_MODE off - product
    help pages are not the operator's corpus, as in count_documents), or
    {"state": "error", "reason": <code>}. The reason is a fixed code, never
    exception text: it reaches the Owner's view and the alert. Once retrieval
    is required, the help collection is searched with the rest - the help lane
    reads it at the same k.

    corpus_expected (the AZ-02 security read): a deployment whose corpus is
    never legitimately empty passes True, and an empty store reads error -
    without it a store that came back empty (a volume not mounted, an index
    wiped) read "not_required" and readiness stayed green, the gap again.
    The search asks for distances only, so no corpus text is pulled.

    READ-ONLY on the vector store, by rule: a write-sick HNSW index crashes
    the process on a write while every read stays green, so a write probe
    would be the outage it was meant to report. Called from the self-check
    timer, never from a request (app/self_check.py)."""
    global _lane_failed_last
    try:
        searched = []
        for c in client.list_collections():
            n = c.count()
            if n > 0:
                searched.append((c, n))
    except Exception:
        return {"state": "error", "reason": "vector_store_unreadable"}
    populated = [c for c, _ in searched if c.name != HELP_COLLECTION]
    if not populated and not rag_only and not corpus_expected:
        return {"state": "not_required"}
    # The embed service is asked first, even for an empty store a corpus is
    # expected in: a broken embed is also what leaves a store empty, and its
    # reason is the one an operator can act on (the 2026-10-08 read - the
    # store's emptiness used to be reported in its place).
    try:
        vector = _embed(_LANE_PROBE_TEXT, retries=1, timeout=15)
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", None)
        return {"state": "error",
                "reason": "embed_model_missing" if code == 404 else "embed_refused"}
    except Exception:
        return {"state": "error", "reason": "embed_unreachable"}
    if (not isinstance(vector, list) or not vector
            or not all(isinstance(x, (int, float)) for x in vector)):
        return {"state": "error", "reason": "embed_malformed"}
    if not populated and corpus_expected:
        return {"state": "error", "reason": "vector_store_empty"}
    k = _fetch_k(RERANK_FETCH)
    failed = []
    for col, n in searched:
        try:
            _knn(col, vector, min(k, n), ["distances"], "probe_retrieval_lane")
        except Exception as e:
            failed.append((col.name, f"{type(e).__name__}: {str(e)[:160]}"))
    names = tuple(name for name, _ in failed)
    if names and names != _lane_failed_last:
        log.warning("probe_retrieval_lane: the search failed on %s",
                    "; ".join(f"{name} ({err})" for name, err in failed))
    _lane_failed_last = names
    if failed:
        return {"state": "error", "reason": "vector_search_failed"}
    return {"state": "ok"}


def _stem(token: str) -> str:
    """Light suffix stripper for BM25 tokens. Deliberately NOT a full
    stemmer - a handful of predictable rules, applied identically to
    documents and queries, so 'plugs' matches 'plug' and 'restarted' matches
    'restart' (the measured vocabulary-mismatch class). Length guards keep
    short words and 'string'/'thing'-type false suffixes intact; digits never
    match a rule, so identifiers (IPs, versions) pass through untouched.
    Over-stemming an occasional word is fine: both sides stem the same way
    and the cross-encoder reranker arbitrates the final order."""
    t = token
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"                       # stories -> story
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        t = t[:-1]                                # machines -> machine
    if len(t) > 6 and t.endswith("ing"):
        t = t[:-3]                                # restarting -> restart
    elif len(t) > 5 and t.endswith("ed"):
        t = t[:-2]                                # restarted -> restart
    if len(t) > 2 and t[-1] == t[-2] and t[-1].isalpha():
        t = t[:-1]                                # shipped -> shipp -> ship
    if len(t) > 4 and t.endswith("e"):
        t = t[:-1]                                # change/changed both -> chang
    return t


def _tokenize(text: str) -> list[str]:
    # Guard None/empty: a chunk read mid-re-embed (department move) can
    # momentarily carry no text, and one None crashes the whole BM25 leg -
    # a chat 500 during any re-embed window.
    return [_stem(t) for t in re.findall(r'\w+', (text or "").lower())]


def _bm25_scores(query: str, docs: list[str], k1: float = BM25_K1, b: float = BM25_B) -> list[float]:
    tokenized_docs = [_tokenize(d) for d in docs]
    # 1.0 when every candidate has no word characters (punctuation-only or
    # empty chunks): avg_dl 0 divided below. The full-corpus leg guards the
    # same way.
    avg_dl = (sum(len(d) for d in tokenized_docs) / max(len(tokenized_docs), 1)) or 1.0
    query_terms = _tokenize(query)

    idf: dict[str, float] = {}
    N = len(docs)
    for term in set(query_terms):
        df = sum(1 for d in tokenized_docs if term in d)
        idf[term] = math.log((N - df + 0.5) / (df + 0.5) + 1)

    scores = []
    for doc_tokens in tokenized_docs:
        dl = len(doc_tokens)
        score = 0.0
        tf_map: dict[str, int] = {}
        for t in doc_tokens:
            tf_map[t] = tf_map.get(t, 0) + 1
        for term in query_terms:
            tf = tf_map.get(term, 0)
            score += idf.get(term, 0) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
        scores.append(score)
    return scores


# --- Full-corpus BM25 leg ----------------------------------------------------
# query_similar's vector fetch can NEVER surface a chunk the embedding missed,
# and _bm25_scores only re-ranks that vector pool - so an exact-identifier
# query ("192.0.2.10") or a vocabulary-mismatched fact is unreachable: a
# pool-entry failure, not a ranking failure. This index scores BM25 over the
# WHOLE collection so the top lexical hits can be unioned into the candidate
# pool before fusion/rerank.
#
# Cached per collection in this process; every KB write goes through
# add_document/delete_source below, which invalidate it. A second process
# builds its own fresh index per run.
_LEX_INDEX: dict[str, dict] = {}


def _invalidate_lexical_index(col_name: str):
    _LEX_INDEX.pop(col_name, None)


def _get_lexical_index(col) -> dict:
    """Build (or return the cached) tokenized index for one collection:
    per-doc term frequencies + doc lengths + document frequencies. Texts and
    embeddings are NOT kept resident - rescued chunks are fetched by id."""
    cached = _LEX_INDEX.get(col.name)
    if cached is not None:
        return cached
    res = col.get(include=["documents"])  # ids are always returned
    ids = list(res.get("ids") or [])
    docs = res.get("documents") or []
    tf: list[dict[str, int]] = []
    dl: list[int] = []
    df: dict[str, int] = {}
    for doc in docs:
        tokens = _tokenize(doc or "")
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        tf.append(counts)
        dl.append(len(tokens))
        for t in counts:
            df[t] = df.get(t, 0) + 1
    idx = {
        "ids": ids, "tf": tf, "dl": dl, "df": df,
        "N": len(ids), "avg_dl": (sum(dl) / len(dl)) if dl else 1.0,
    }
    _LEX_INDEX[col.name] = idx
    return idx


def _lexical_top_ids(query: str, col, limit: int, exclude: set[str]) -> list[str]:
    """Top-`limit` chunk ids in the collection by full-corpus Okapi BM25,
    skipping ids already in the vector candidate set. Only positive scores
    qualify (at least one query term must appear in the chunk)."""
    idx = _get_lexical_index(col)
    if not idx["N"]:
        return []
    terms = _tokenize(query)
    N, avg_dl = idx["N"], idx["avg_dl"]
    idf = {t: math.log((N - idx["df"].get(t, 0) + 0.5) / (idx["df"].get(t, 0) + 0.5) + 1)
           for t in set(terms)}
    scored: list[tuple[float, str]] = []
    for i, doc_id in enumerate(idx["ids"]):
        if doc_id in exclude:
            continue
        counts, doc_len = idx["tf"][i], idx["dl"][i]
        score = 0.0
        for term in terms:
            f = counts.get(term, 0)
            if f:
                score += idf[term] * (f * (BM25_K1 + 1)) / (
                    f + BM25_K1 * (1 - BM25_B + BM25_B * doc_len / avg_dl))
        if score > 0:
            scored.append((score, doc_id))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [doc_id for _, doc_id in scored[:limit]]


def _cosine_distance(a, b) -> float:
    """Chroma-convention cosine distance (1 - cosine similarity) between a
    query embedding and a stored one, so BM25-rescued chunks get a REAL
    vector score for fusion and the rag threshold - not a fabricated floor.
    Neutral 1.0 on a zero vector (e.g. a mocked test embedder)."""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 1.0
    return 1.0 - dot / math.sqrt(na * nb)


def _recency_multiplier(meta: dict) -> float:
    """Age-decay for chunks stamped with entry_date (dated log entries).
    max(RECENCY_FLOOR, 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)); 1.0 for
    anything without a parseable entry_date, so fact docs are unaffected."""
    d = (meta or {}).get("entry_date")
    if not d:
        return 1.0
    try:
        age_days = (datetime.now(timezone.utc).date() - date.fromisoformat(str(d))).days
    except (ValueError, TypeError):
        return 1.0
    if age_days <= 0:
        return 1.0
    return max(RECENCY_FLOOR, 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))


def _hybrid_rank(docs: list, distances: list, metadatas: list, query: str, n_results: int) -> list[dict]:
    """Apply BM25 reranking to a combined result set and return top n_results.
    Recency-weighted: the ORDER (which candidates survive into the reranker's
    pool) decays with entry_date age; the returned similarity score does not,
    so the rag threshold keeps its meaning."""
    # A GHOST is an id the index returned and the store no longer holds: no
    # text, no metadata. A serving process answers with ghosts after a SECOND
    # process deleted pieces under it (a re-seed or a re-ingest run beside the
    # server) and until it restarts - reproduced 2026-09-28 on a throwaway
    # store. The tokenizer reads a missing text as empty, but the result built
    # below read the missing metadata and raised: a failed search for every
    # question near a ghost. Dropped here (2026-10-02), so the window costs a
    # thinner answer and not an error; a live piece whose metadata is missing
    # reads as having none.
    rows = list(zip(docs, distances, metadatas))
    live = [(d, dist, m if isinstance(m, dict) else {})
            for d, dist, m in rows if isinstance(d, str) and d]
    if len(live) != len(rows):
        log.warning("retrieval dropped %d result(s) with no document - the index "
                    "holds ids the store does not; restart to reload it",
                    len(rows) - len(live))
    docs = [x[0] for x in live]
    distances = [x[1] for x in live]
    metadatas = [x[2] for x in live]
    if not docs:
        return []

    vector_scores = [1 - (d / 2) for d in distances]
    max_v = max(vector_scores) or 1
    norm_vector = [s / max_v for s in vector_scores]

    bm25 = _bm25_scores(query, docs)
    max_b = max(bm25) or 1
    norm_bm25 = [s / max_b for s in bm25]

    combined = sorted(
        zip(docs, metadatas, vector_scores,
            [(VECTOR_WEIGHT * v + BM25_WEIGHT * b) * _recency_multiplier(m)
             for v, b, m in zip(norm_vector, norm_bm25, metadatas)]),
        key=lambda x: x[3],
        reverse=True
    )

    return [
        {"text": doc, "source": meta.get("source", "unknown"), "score": round(vs, 4),
         # DB-truth generated chunks (app/system_records.py stamps auto_generated on
         # ingest); carried through so retrieval can rank them first on status
         # questions and the context builder can mark them authoritative.
         "auto_generated": meta.get("auto_generated") == "true",
         # Provenance trust tier (injection gate): stamped at ingest, derived
         # for pre-gate chunks (rag_config.derive_trust). format_context
         # labels and orders the context by it; the prompt's
         # data-not-instructions rules enforce it at answer time.
         "trust": derive_trust(meta),
         "injection_flagged": (meta or {}).get("injection_flagged") == "true"}
        for doc, meta, vs, _ in combined[:n_results]
    ]


def _gate_chunk(doc_id: str, text: str, metadata: dict, department: str | None,
                quarantine_exempt: bool) -> dict:
    """The UNTRUSTED-CORPUS INJECTION GATE, factored so BOTH write shapes -
    single add_document and the batched add_documents_batch - run the same
    checks (one gate; a second write path must never become a second choke
    point). Returns the stamped metadata; raises QuarantinedContent for hot
    untrusted content (fail-closed)."""
    from app import corpus_scan
    meta = dict(metadata or {})
    trust = derive_trust(meta)
    meta["trust"] = trust
    if corpus_scan.INJECTION_SCAN_MODE != "off":
        already_tagged = meta.get("injection_flagged") == "true"
        findings = corpus_scan.scan(text)
        if findings:
            if (not quarantine_exempt
                    and corpus_scan.should_quarantine(trust, findings)):
                raise corpus_scan.QuarantinedContent(
                    meta.get("source", doc_id), department, trust, text, findings)
            meta["injection_flagged"] = "true"
            meta["injection_types"] = corpus_scan.finding_types(findings)
            # Curated/system hits are expected (the corpus quotes attack
            # strings); log only the tiers where a finding is signal - and
            # only when the calling endpoint hasn't already recorded it
            # (per-doc, not per-chunk).
            if trust in corpus_scan.UNTRUSTED_TIERS and not already_tagged:
                from app.logger import log as event_log
                event_log("injection_detected", source=meta.get("source", doc_id),
                          trust=trust, types=meta["injection_types"],
                          quarantined=False, mode=corpus_scan.INJECTION_SCAN_MODE)
        elif not already_tagged:
            # Written both ways (2026-10-29). Chunk ids are content-addressed
            # and chroma's upsert and update MERGE metadata, so while the gate
            # only ever ADDED "true", a chunk a later write kept carried an
            # earlier write's tag for good - the attack line gone from the
            # document, the kept chunks still labeled flagged in the context
            # and listed as flagged sources. A clean chunk with no writer
            # "true" is stamped "false", its types emptied; a writer's
            # document-level "true" still wins (already_tagged). Every reader
            # compares == "true".
            meta["injection_flagged"] = "false"
            meta["injection_types"] = ""
    return meta


def add_document(doc_id: str, text: str, metadata: dict, department: str | None = None,
                 quarantine_exempt: bool = False):
    """Index one chunk - and run the UNTRUSTED-CORPUS INJECTION GATE.

    The gate itself lives in _gate_chunk, shared by BOTH write shapes: single
    writes land here (file watcher, system-record producer, ingest/upload endpoints)
    while multi-chunk writes land in add_documents_batch (connector modules
    and the file delta path) - so the gate cannot be walked around: each
    chunk gets its provenance trust tier stamped, is scanned for
    injection-shaped content, and hot UNTRUSTED content raises
    QuarantinedContent instead of being indexed (fail-closed; the ingest
    endpoints catch it and write a review row). The owner's own
    curated/system content is TAGGED, never withheld - the corpus
    legitimately quotes injection strings. quarantine_exempt=True is the
    owner-reviewed release path: the tag stays (audit), the block is waived.
    """
    meta = _gate_chunk(doc_id, text, metadata, department, quarantine_exempt)
    col = _get_collection(department)
    embedding = _embed(text)
    _drop_held_rows(col, [doc_id])
    col.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[meta]
    )
    _invalidate_lexical_index(col.name)


def _drop_held_rows(col, ids: list[str]) -> None:
    """Delete the rows already stored under any of ids, so the upsert that
    follows REPLACES them (2026-10-29). The store's upsert MERGES metadata on
    a stored id: a key the earlier row held and this write does not set
    survived under the new text. A diffing writer reads its own source by
    `source`, so a row a manage_kb holder pre-wrote through /api/ingest under
    a future chunk's id with ANOTHER source read as a new id to it, and the
    add kept that caller's keys - an entry_date that demotes the chunk in
    retrieval, a pii_flagged that lists the writer's source among the PII
    sources. Every add goes through here, /api/ingest's re-send of its own id
    included: the row is what this write says, nothing of an earlier one.

    Called after the embed and right before the upsert, so a failed embed
    deletes nothing. The cost is one ids-only read per call (per slice in the
    batch): the writers pass only the ids their diff found new, so an
    unchanged re-sync never reaches it, and the delete runs only for an id a
    row holds (a delete of an absent id would log a warning per id)."""
    held = col.get(ids=ids, include=[])["ids"]
    if held:
        col.delete(ids=list(held))


# One embed call + one upsert per slice of this many chunks: a connector sync
# can push hundreds of chunks through what would otherwise be one embed round
# trip + one upsert PER CHUNK. 64 keeps the /api/embed payload comfortably
# small.
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "64"))


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embeddings: ONE HTTP round trip via Ollama's /api/embed (accepts
    an input array) instead of one /api/embeddings call per text. Falls back
    to the per-text legacy endpoint - which carries the retry/backoff - when
    the batch endpoint is unavailable or answers with an unexpected shape."""
    if not texts:
        return []
    try:
        # Timeout SCALES with the batch's PAYLOAD: Ollama computes the input
        # array sequentially, and embed time tracks TOKENS, not text count -
        # a flat per-text budget still times out on a slice of big chunks,
        # and then the serial fallback pays the full cost AGAIN on top (the
        # double-pay this formula exists to kill). Budget = 30s floor + the
        # larger of 12s/text or 1s per 250 chars. Known limit, on the
        # record: the budget also pays QUEUE WAIT behind earlier batches in
        # a single-lane embed server, and no payload-derived formula can
        # cover lane contention - if that bites, the fix space is the lane,
        # not another coefficient.
        response = requests.post(
            f"{EMBED_BASE}/api/embed",
            json={"model": EMBED_MODEL, "input": texts},
            timeout=30 + max(12 * len(texts),
                             sum(len(t) for t in texts) // 250),
        )
        response.raise_for_status()
        embs = response.json().get("embeddings")
        if isinstance(embs, list) and len(embs) == len(texts):
            return embs
        log.warning("_embed_batch: unexpected /api/embed response shape; "
                    "falling back to per-text embeddings")
    except Exception as e:
        log.warning("_embed_batch: batch endpoint failed (%s); "
                    "falling back to per-text embeddings", e)
    return [_embed(t) for t in texts]


def add_documents_batch(entries: list[tuple[str, str, dict]],
                        department: str | None = None,
                        quarantine_exempt: bool = False) -> int:
    """Batched ingestion for multi-chunk writes (connector modules and the
    file delta path).

    Every chunk passes the SAME gate as add_document (_gate_chunk), and the
    whole batch is gated BEFORE anything is written - so a hot chunk
    withholds the entire file with zero partial index (no chunk-boundary
    half-ingested state). Then one embed call + one upsert per
    EMBED_BATCH_SIZE slice. Returns chunks written; QuarantinedContent
    propagates for the caller to quarantine the file (the upload/connector
    pattern)."""
    if not entries:
        return 0
    ids: list[str] = []
    texts: list[str] = []
    metas: list[dict] = []
    for doc_id, text, metadata in entries:
        meta = _gate_chunk(doc_id, text, metadata, department, quarantine_exempt)
        ids.append(doc_id)
        texts.append(text)
        metas.append(meta)
    col = _get_collection(department)
    for start in range(0, len(ids), EMBED_BATCH_SIZE):
        sl = slice(start, start + EMBED_BATCH_SIZE)
        embeddings = _embed_batch(texts[sl])
        _drop_held_rows(col, ids[sl])   # a write replaces, never merges (2026-10-29)
        col.upsert(ids=ids[sl], embeddings=embeddings,
                   documents=texts[sl], metadatas=metas[sl])
    _invalidate_lexical_index(col.name)
    return len(ids)


# The gate's injection verdict on a chunk. A kept row's other keys are compared
# with the writer's exactly (_kept_metadata_differs); these two follow
# _scan_tags_differ, which reads the verdict as its readers do and scans only
# where the answer can differ.
_INJECTION_KEYS = ("injection_flagged", "injection_types")


def _kept_metadata_differs(stored: dict, metadata: dict | None, text: str) -> bool:
    """Whether a kept row's stored metadata is not exactly what this writer
    writes for it (2026-10-29). Every key but the injection verdict is
    compared as stored: an extra key, a missing one or another value - the
    chunk index, the from_file stamp, auto_generated, the PII tag, the tier
    (_gate_chunk stamps derive_trust of the writer's metadata, so that is the
    value compared). Until this, the restamp compared the stamp, the tier and
    the scan tags alone, so a key a caller pre-wrote under a future chunk's
    exact text and the writer does not write - an entry_date that demotes
    the chunk in retrieval, a pii_flagged with the PII scan off - read as the
    writer's. The injection verdict is _scan_tags_differ's."""
    want = dict(metadata or {})
    want["trust"] = derive_trust(want)
    if ({k: v for k, v in stored.items() if k not in _INJECTION_KEYS}
            != {k: v for k, v in want.items() if k not in _INJECTION_KEYS}):
        return True
    return _scan_tags_differ(stored, metadata, text)


def _scan_tags_differ(stored: dict, metadata: dict | None, text: str) -> bool:
    """Whether a kept row's injection tag, read as == "true" on both sides,
    is not what this write decides for it (2026-10-29) - the part of
    _kept_metadata_differs that a plain comparison cannot make without a
    scan. The PII tag is the writer's per document and is compared there
    exactly, with every other key (2026-10-29: here, a writer that carried
    none - the PII scan off - left a stored "true" standing).

    The injection verdict is _gate_chunk's: the writer's document-level
    "true", else the chunk's own scan; with the scan off the gate stamps
    nothing of its own and a writer's value passes through, so a stored
    "true" the writer does not write differs (2026-10-29: until then it stood
    unless the writer named the key). Cheapest first, scanning only where the
    answer can differ:
      - the writer says "true": decided "true", no scan;
      - the row reads not "true" and the writer does not either: no scan. The
        row was gated over this same text, so its tag is that text's verdict;
        a verdict that moved since (new patterns, the scan switched on) is
        out of scope here, as before this comparison existed, and reaches the
        row when its text next changes;
      - the row reads "true" and the writer does not: one scan of that row's
        text - it stays "true" only if its own text is hot.
    So an everyday re-sync of a file with no flagged chunk still pays one
    metadata read per slice, no scan and no write; a file with flagged
    sections pays one regex scan per flagged kept chunk, and writes nothing
    while those chunks are still hot."""
    m = metadata or {}
    held = stored.get("injection_flagged") == "true"
    if m.get("injection_flagged") == "true":
        return not held
    from app import corpus_scan
    if corpus_scan.INJECTION_SCAN_MODE == "off":
        return held
    if not held:
        return False
    return not corpus_scan.scan(text)


def _rows_by_id(got: dict) -> dict[str, tuple[str | None, dict, object]]:
    """A get's rows as id -> (text, metadata, vector). The vector is None when
    the get did not include it; chroma answers a 2-D numpy array, so it is
    tested against None and never for truth."""
    vectors = got.get("embeddings")
    if vectors is None:
        vectors = [None] * len(got["ids"])
    return {i: (d, m or {}, v) for i, d, m, v in
            zip(got["ids"], got["documents"], got["metadatas"], vectors)}


def restamp_reused_chunks(entries: list[tuple[str, str, dict]],
                          department: str | None = None,
                          quarantine_exempt: bool = False,
                          rewritten: list[str] | None = None,
                          stored_rows: dict | None = None) -> int:
    """A writer owns every chunk it writes, the ids it REUSED included.

    Chunk ids are content-addressed with one formula for every writer, and the
    delta diff skips an id already indexed - so a write whose chunk text
    matched a chunk already stored under the same name wrote nothing to that
    row, and the row kept the previous writer's metadata. When that writer was
    the knowledge file sync, an upload's row kept from_file="true", the source
    still read as wholly file-owned, and the knowledge orphan prune deleted the
    upload (2026-10-29). The reverse the same day: a knowledge file synced over
    an upload's identical text kept the upload's rows - no stamp, the
    uploader's tier - so the file sync passes the ids it kept here too.

    entries are the (id, text, metadata) the writer would write for the ids it
    kept. A kept row ends this call holding exactly the metadata the writer
    writes (2026-10-29), as a fresh write of it would: _kept_metadata_differs
    compares every stored key with the writer's - an extra key, a missing
    one, another value - and the injection tag by _scan_tags_differ. A row
    already the writer's is left as it is, so a re-sync of an unchanged file
    pays one read per slice and no write. A row that differs gets the
    writer's metadata through the same gate as add_document, every such row
    gated before anything is written, and nothing is re-embedded (same text,
    same vector):
      - when the row holds no key the writer does not write, by update - the
        store MERGES the given metadata, so the row then holds exactly it
        (an edit above a section moves the kept rows' chunk index this way,
        one metadata update per moved row, the vectors untouched);
      - when it holds such a key, WHOLE: deleted, then added with the
        writer's metadata and its STORED vector, since a merge cannot remove
        a key and the store's API refuses None. Until 2026-10-29 every row
        went by update with from_file overwritten "false", so a key a caller
        pre-wrote under a future chunk's exact text and the writer does not
        write - an entry_date that demotes the chunk in retrieval, a
        pii_flagged with the PII scan off - stayed under the writer's stamp.
    The writer's metadata is written as it is, so a row of a writer with no
    from_file holds none (it read "false" before; every reader compares
    == "true"). Reads and writes go in EMBED_BATCH_SIZE slices: the pinned
    chroma refuses one call over its max batch size. Returns the count
    written.

    The read takes each kept row's text, metadata and vector in the SAME get
    per slice: the vector, for a row written whole, is the added cost
    (2026-10-29) - one vector per kept row read, no extra call and no embed.
    stored_rows, when given, is that read already made by the caller over
    its source (id -> (text, metadata, vector), get_source_rows - the system
    records writer), and no get is made here.

    quarantine_exempt is add_document's: the quarantine release passes it
    (2026-10-29), so the Owner's waiver covers the ids the release reuses as
    it covers the ones it adds - the tag stays, the block is waived.

    A kept id is kept only if the stored text IS the writer's (2026-10-29).
    Every rule above assumes it and nothing checked it: the ids are
    content-addressed, so a manage_kb holder who can predict a FUTURE chunk's
    text can write that id first through /api/ingest, with other text and the
    same source (the route's refusal of an id a system writer holds cannot see
    a row that does not exist yet). This restamp then stamped the caller's
    text as the writer's - for the file sync, from_file "true" and the curated
    tier. So a row whose text is not the writer's is written WHOLE with the
    writer's text and a fresh embedding, embedded before anything is written
    or deleted. rewritten, when given, gets the ids of those rows - the
    caller's text, not the writer's - before their delete, for a caller that
    must undo them (the release's rollback); a row of the writer's own text
    written whole for a key is not among them, and its rollback is the
    metadata snapshot's."""
    if not entries:
        return 0
    col = _existing_collection(department)
    if col is None:
        return 0   # nothing indexed here, so nothing was reused
    current: dict[str, tuple[str | None, dict, object]] = {}
    if stored_rows is not None:
        current = {doc_id: stored_rows[doc_id] for doc_id, _, _ in entries
                   if doc_id in stored_rows}
    else:
        for start in range(0, len(entries), EMBED_BATCH_SIZE):
            current.update(_rows_by_id(col.get(
                ids=[doc_id for doc_id, _, _ in entries[start:start + EMBED_BATCH_SIZE]],
                include=["metadatas", "documents", "embeddings"])))
    ids: list[str] = []
    metas: list[dict] = []
    fresh: list[tuple[str, str, dict]] = []           # embedded here
    reuse: list[tuple[str, str, dict, list]] = []     # its stored vector
    foreign: list[str] = []
    for doc_id, text, metadata in entries:
        if doc_id not in current:
            continue
        held_text, old, vector = current[doc_id]
        if held_text != text:
            fresh.append((doc_id, text,
                          _gate_chunk(doc_id, text, metadata, department, quarantine_exempt)))
            foreign.append(doc_id)
            continue
        if not _kept_metadata_differs(old, metadata, text):
            continue
        meta = _gate_chunk(doc_id, text, metadata, department, quarantine_exempt)
        if not set(old) - set(meta):
            ids.append(doc_id)
            metas.append(meta)
        elif vector is None:
            fresh.append((doc_id, text, meta))   # no stored vector to reuse
        else:
            reuse.append((doc_id, text, meta,
                          vector.tolist() if hasattr(vector, "tolist") else list(vector)))
    if foreign:
        log.warning("restamp: %d kept id(s) in %s held text that is not the "
                    "writer's; written whole", len(foreign), col.name)
    embeddings: list[list[float]] = []
    for start in range(0, len(fresh), EMBED_BATCH_SIZE):
        embeddings += _embed_batch([t for _, t, _ in fresh[start:start + EMBED_BATCH_SIZE]])
    if rewritten is not None:
        rewritten.extend(foreign)
    whole = [(i, t, m, e) for (i, t, m), e in zip(fresh, embeddings)] + reuse
    for start in range(0, len(whole), EMBED_BATCH_SIZE):
        sl = whole[start:start + EMBED_BATCH_SIZE]
        col.delete(ids=[w[0] for w in sl])
        col.upsert(ids=[w[0] for w in sl], embeddings=[w[3] for w in sl],
                   documents=[w[1] for w in sl], metadatas=[w[2] for w in sl])
    if whole:
        _invalidate_lexical_index(col.name)
    for start in range(0, len(ids), EMBED_BATCH_SIZE):
        sl = slice(start, start + EMBED_BATCH_SIZE)
        col.update(ids=ids[sl], metadatas=metas[sl])
    return len(ids) + len(whole)


def chunk_metadata(ids: list[str], department: str | None = None) -> dict[str, dict]:
    """The stored metadata of each id that is indexed, keyed by id; an id that
    is not stored is absent. The quarantine release's snapshot before it
    re-stamps the ids it reuses (2026-10-29), for restore_chunk_metadata to
    put back if the release then fails; and /api/ingest's read of the one id
    it was sent, to refuse an id a system writer holds (2026-10-29). Read in
    EMBED_BATCH_SIZE slices."""
    col = _existing_collection(department) if ids else None
    if col is None:
        return {}
    out: dict[str, dict] = {}
    for start in range(0, len(ids), EMBED_BATCH_SIZE):
        got = col.get(ids=ids[start:start + EMBED_BATCH_SIZE], include=["metadatas"])
        out.update({i: dict(m) for i, m in zip(got["ids"], got["metadatas"]) if m})
    return out


def restore_chunk_metadata(snapshot: dict[str, dict], department: str | None = None) -> int:
    """Write a chunk_metadata snapshot back: every key each row held returns
    to its value - the from_file stamp and the trust tier among them. A key
    written since that the row did not hold stays (chroma's update MERGES
    metadata and its API refuses None, so a key cannot be removed). No
    re-embed. Returns the count written."""
    col = _existing_collection(department) if snapshot else None
    if col is None:
        return 0
    ids = list(snapshot)
    for start in range(0, len(ids), EMBED_BATCH_SIZE):
        sl = ids[start:start + EMBED_BATCH_SIZE]
        col.update(ids=sl, metadatas=[snapshot[i] for i in sl])
    return len(ids)


class RetrievalUnavailable(RuntimeError):
    """Retrieval is DOWN, not empty - an outage.

    Exists so callers can tell an infrastructure outage from "the corpus has
    nothing on this". Without it the two are the same value ([]), and the chat
    path speaks an outage to the user as a confident "I don't have that
    information". Raised on an outage - the query could not be embedded, or a collection
    failed and none that answered returned a candidate; a partial failure returns the
    surviving candidates, because those are real.
    """


def _fetch_k(n_results: int) -> int:
    """How many candidates query_similar asks each collection for: twice what
    its caller keeps, never fewer than ten. The lane probe asks each
    collection for what the chat lane asks (probe_retrieval_lane), through
    this same function, so the two cannot drift apart."""
    return max(n_results * 2, 10)


def _knn(col, embedding: list[float], k: int, include: list[str], caller: str) -> dict:
    """One collection's vector search at k - query_similar and the lane probe
    both search through here, so the probe proves the search a question runs.

    hnswlib refuses a knn query whose k approaches the index's element count
    ("Cannot return the results in a contigious 2D array" - typo verbatim): a
    small HNSW graph is not guaranteed traversable to every node. k halves and
    the search runs again until it is accepted, rather than losing the
    collection's whole vector leg (and in query_similar its BM25 leg) - a
    silent per-collection loss that reads as "no knowledge found". Any other
    failure, or a refusal at k=1, raises."""
    while True:
        try:
            return col.query(query_embeddings=[embedding], n_results=k, include=include)
        except Exception as e:
            if k <= 1 or "contigious 2D array" not in str(e):
                raise
            log.warning("%s: %s knn refused k=%d (%s), retrying k=%d",
                        caller, getattr(col, "name", "?"), k, e, k // 2)
            k //= 2


def query_similar(query: str, n_results: int = 5,
                  department: str | list[str] | None = None,
                  only_department: bool = False) -> list[dict]:
    """Hybrid vector + BM25 search. Merges the global KB with each department
    KB given. Accepts a single department or a list (e.g. the user's own
    department plus a query-routed one - see app/routing.py); the global
    collection is always queried.

    only_department=True reads the named department's collection ALONE, with
    no global merge - the help lane (2026-09-21): product help must never be
    answered from the operator's documents, and it is the one caller that
    wants exactly one collection.

    Raises RetrievalUnavailable on an outage - the query could not be
    embedded, or a collection failed and none that answered returned a
    candidate - which each caller must say as one, never as an empty result.
    A collection that failed beside one that returned candidates costs only
    its own candidates."""
    try:
        embedding = _embed(query)
    except Exception as e:
        # Nothing can be searched without the query's vector: a TOTAL outage,
        # said as one - not a bare error that hides the cause, and never an
        # empty result read as "nothing on this".
        raise RetrievalUnavailable(
            "retrieval unavailable: the query could not be embedded (%s)"
            % type(e).__name__) from e
    fetch_k = _fetch_k(n_results)

    all_docs: list[str] = []
    all_distances: list[float] = []
    all_metas: list[dict] = []
    failed_collections: list[str] = []

    departments = [department] if isinstance(department, str) else list(department or [])
    collections_to_query = [] if only_department else [_get_collection()]  # global unless scoped
    seen_names = {GLOBAL_COLLECTION}
    for dept in departments:
        if not dept or dept == "general":
            continue
        col = _existing_collection(dept)
        if col is None:
            continue  # nothing indexed there; asking must not create it
        if col.name not in seen_names:
            seen_names.add(col.name)
            collections_to_query.append(col)

    for col in collections_to_query:
        try:
            count = col.count()
            if count == 0:
                continue
            # The search halves k on hnswlib's refusal of a k near the
            # index's size (_knn); any other failure lands in the except
            # below.
            results = _knn(col, embedding, min(fetch_k, count),
                           ["documents", "distances", "metadatas"], "query_similar")
            vector_ids = results["ids"][0] if results.get("ids") else []
            all_docs.extend(results["documents"][0] if results["documents"] else [])
            all_distances.extend(results["distances"][0] if results["distances"] else [])
            all_metas.extend(results["metadatas"][0] if results["metadatas"] else [])
        except Exception as e:
            # Don't hide infrastructure failures behind an empty result - a
            # down/corrupt Chroma otherwise reads as "no knowledge found" in
            # chat.
            #
            # Logging and continuing alone IS hiding it: the caller receives
            # [] and cannot tell "the corpus has nothing on this" from "the
            # index is down", and in RAG_ONLY_MODE the chat path turns [] into
            # its refusal - an OUTAGE spoken to the user as a confident claim
            # about the corpus. So each failure is counted here and a TOTAL
            # failure raises below. A PARTIAL failure still degrades quietly
            # on purpose: the surviving collections returned real candidates,
            # so the answer is narrower rather than wrong.
            failed_collections.append(getattr(col, "name", "?"))
            log.warning("query_similar: query failed on collection %s: %s", getattr(col, "name", "?"), e)
            continue

        # Full-corpus BM25 leg: union in the top lexical hits the embedding
        # never surfaced. Rescued chunks get their true cosine distance (from
        # the stored embedding) so fusion and the rag threshold treat them
        # exactly like vector-fetched candidates. The leg failing must never
        # break retrieval - the vector results above still stand.
        if BM25_FETCH <= 0:
            continue
        try:
            rescued = _lexical_top_ids(query, col, BM25_FETCH, exclude=set(vector_ids))
            if not rescued:
                continue
            got = col.get(ids=rescued, include=["documents", "metadatas", "embeddings"])
            for doc, meta, emb in zip(got.get("documents") or [],
                                      got.get("metadatas") or [],
                                      got.get("embeddings") if got.get("embeddings") is not None else []):
                all_docs.append(doc)
                all_distances.append(_cosine_distance(embedding, emb))
                all_metas.append(meta)
        except Exception as e:
            log.warning("query_similar: BM25 leg failed on collection %s: %s",
                        getattr(col, "name", "?"), e)

    # An outage, not an empty corpus, whenever a collection failed and no
    # collection that answered returned a candidate: every one failed, or the
    # ones that did not are empty (an empty collection is skipped above, so a
    # failed department beside an empty global would otherwise come back as
    # [] and be narrated as missing knowledge). A failure beside a collection
    # that returned candidates stays partial: those candidates are real.
    if failed_collections and not all_docs:
        raise RetrievalUnavailable(
            "retrieval unavailable: %d of %d collection(s) failed (%s) and none "
            "that answered returned a candidate"
            % (len(failed_collections), len(collections_to_query),
               ", ".join(failed_collections)))

    return _hybrid_rank(all_docs, all_distances, all_metas, query, n_results)


def list_sources(department: str | None = None) -> list[dict]:
    """List ingested sources. department=None returns all collections merged
    with a dept label."""
    if department is not None:
        # List only the specified department's collection
        col = _existing_collection(department)
        if col is None:
            return []
        results = col.get(include=["metadatas"])
        counts: dict[str, int] = {}
        for meta in results["metadatas"]:
            source = meta.get("source", "unknown")
            counts[source] = counts.get(source, 0) + 1
        dept_label = department if department else "general"
        return [{"source": s, "count": c, "department": dept_label} for s, c in sorted(counts.items())]

    # Aggregate across all collections
    all_collections = [GLOBAL_COLLECTION] + [
        c.name for c in client.list_collections() if _is_corpus_collection(c.name)
    ]
    merged: dict[tuple[str, str], int] = {}
    for col_name in all_collections:
        dept_label = "general" if col_name == GLOBAL_COLLECTION else col_name[3:]
        try:
            col = client.get_or_create_collection(name=col_name, metadata=collection_metadata())
            results = col.get(include=["metadatas"])
            for meta in results["metadatas"]:
                source = meta.get("source", "unknown")
                merged[(source, dept_label)] = merged.get((source, dept_label), 0) + 1
        except Exception:
            pass

    return [
        {"source": s, "count": c, "department": d}
        for (s, d), c in sorted(merged.items())
    ]


def list_file_sources() -> list[dict]:
    """Every indexed source with its chunk count AND how many of those chunks
    the file sync wrote (metadata from_file="true", stamped by
    ingest_sync._ingest_file_locked), per department. The knowledge orphan
    prune reads it, and must touch only sources the file sync wholly owns: an
    upload is named by its file name - the same shape as a knowledge file -
    and carries no from_file stamp, and neither do API ingests or generated
    records.

    Enumerated through _is_corpus_collection like every corpus-wide listing:
    the help pages carry the same from_file stamp and have no file under
    KNOWLEDGE_DIR, so a listing that reached kb_help would offer every help
    page to the prune. A collection that cannot be read is skipped (its
    sources are simply not candidates), never guessed at."""
    names = [GLOBAL_COLLECTION] + [
        c.name for c in client.list_collections() if _is_corpus_collection(c.name)
    ]
    merged: dict[tuple[str, str], list[int]] = {}
    for col_name in names:
        dept_label = "general" if col_name == GLOBAL_COLLECTION else col_name[3:]
        col = _existing_collection(None if col_name == GLOBAL_COLLECTION else dept_label)
        if col is None:
            continue
        try:
            metas = col.get(include=["metadatas"])["metadatas"]
        except Exception as e:
            log.warning("list_file_sources: collection %s unreadable, its sources "
                        "are not prune candidates: %s", col_name, e)
            continue
        for meta in metas:
            m = meta or {}
            row = merged.setdefault((m.get("source", "unknown"), dept_label), [0, 0])
            row[0] += 1
            if m.get("from_file") == "true":
                row[1] += 1
    return [{"source": s, "department": d, "count": c, "file_chunks": f}
            for (s, d), (c, f) in sorted(merged.items())]


def corpus_fingerprint() -> str:
    """A compact, deterministic identity for the corpus AS IT IS RIGHT NOW.

    Why this exists: an eval score is not a property of the system; it is a
    property of (system, corpus, question set). Pinning the writer model and
    the question set still leaves the corpus unrecorded - two runs can differ
    by points with no record that the thing being measured moved underneath
    them. The fingerprint does NOT make two such runs comparable - nothing
    can, after the fact. It makes their INCOMPARABILITY visible instead of
    silent, the same bargain the writer-model stamp strikes.

    Cheap by construction: derived from the already-materialised source/count
    listing, so it re-embeds nothing and re-reads no documents. Sorted before
    hashing so the value depends on corpus CONTENT SHAPE, not on dict or
    collection iteration order. Never raises - an unavailable fingerprint is
    recorded as such, because a run that cannot identify its corpus must not
    look like one that can.
    """
    try:
        sources = list_sources()
    except Exception as e:  # a stamp is diagnostic; it must never fail a run
        return f"unavailable:{type(e).__name__}"
    triples = sorted((s["source"], s["department"], s["count"]) for s in sources)
    chunks = sum(t[2] for t in triples)
    digest = hashlib.sha256(
        json.dumps(triples, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"src={len(triples)};chunks={chunks};sha={digest}"


def delete_source(source: str, department: str | None = None):
    col = _existing_collection(department)
    if col is None:
        return  # nothing to delete from, and asking must not create one
    results = col.get(where={"source": source}, include=["metadatas"])
    if results["ids"]:
        col.delete(ids=results["ids"])
        _invalidate_lexical_index(col.name)


def delete_file_chunks(source: str, department: str | None = None) -> int:
    """Delete only the chunks of a source that a file sync wrote - the ones
    stamped from_file="true" - and leave any other chunk under that name.

    delete_source removes every chunk keyed by a name, which is right for
    removing a whole source and wrong for cleaning up one writer's leftovers:
    an upload is keyed by its file name too, so a purge of a stale file-sync
    key through delete_source also deleted any upload that shared the name.
    Returns the count deleted (2026-10-29: a file that now yields no text is
    removed through here, and logged only when something went)."""
    col = _existing_collection(department)
    if col is None:
        return 0  # nothing to delete from, and asking must not create one
    results = col.get(where={"$and": [{"source": source}, {"from_file": "true"}]},
                      include=["metadatas"])
    if results["ids"]:
        col.delete(ids=results["ids"])
        _invalidate_lexical_index(col.name)
    return len(results["ids"])


def get_source_ids(source: str, department: str | None = None) -> list[str]:
    """All chunk ids currently indexed for a source. The read half of delta
    ingestion: ids are content-addressed, so comparing this set against the
    desired set yields exactly what to embed and what to drop."""
    col = _existing_collection(department)
    if col is None:
        return []
    return list(col.get(where={"source": source}).get("ids") or [])


def get_source_rows(source: str, department: str | None = None) -> dict[str, tuple[str | None, dict, object]]:
    """get_source_ids with each id's stored text, metadata and vector, as
    id -> (text, metadata, vector): the system-records writer's read. It
    keeps an id only if the stored text is the record's own, and hands the
    kept rows to restamp_reused_chunks (stored_rows) so they end holding
    exactly the record's metadata, a row written whole reusing its vector
    (restamp_reused_chunks says why, 2026-10-29). The same one get by source;
    the metadata and the vectors are its added cost (2026-10-29 - it read
    the text alone, as get_source_documents)."""
    col = _existing_collection(department)
    if col is None:
        return {}
    return _rows_by_id(col.get(where={"source": source},
                               include=["documents", "metadatas", "embeddings"]))


def delete_documents(ids: list[str], department: str | None = None):
    """Delete specific chunks by id. The delete half of delta ingestion -
    delete_source stays for whole-source removal (watcher deletes, purges)."""
    if not ids:
        return
    col = _existing_collection(department)
    if col is None:
        return  # nothing to delete from, and asking must not create one
    col.delete(ids=ids)
    _invalidate_lexical_index(col.name)


def _department_partition() -> tuple[list[str], list[str]]:
    """Split kb_* collections into (real, residue) by whether they hold documents.

    The residue class: delete_source removes DOCUMENTS while the collection
    object survives, so an empty leftover is invisible to an honest
    "residual entries = 0" AND to corpus_fingerprint() (which hashes
    (source, department, count) triples and sees nothing for an empty one) -
    yet it used to surface as an invented department on /api/ingest/departments.
    A collection whose count cannot be read is not provably real, so it goes to
    residue too (fail-closed) rather than riding a department list on faith.
    """
    real, residue = [], []
    for col in client.list_collections():
        if not _is_corpus_collection(col.name):
            continue
        try:
            n = col.count()
        except Exception:
            n = 0
        (real if n > 0 else residue).append(col.name[3:])
    return sorted(real), sorted(residue)


def department_residue() -> list[str]:
    """Department names whose collections exist but hold ZERO documents.

    The observable half of the department-list invariant: residue never
    appears in list_departments(), but it is never silently swallowed either -
    startup reports it, and this seam is what tests and operators check.
    Deleting it is NOT this function's job: a non-empty collection is evidence
    (something other than a clean run put content there), and even an empty one
    is a fact about which tool cleaned up imperfectly. Report, don't destroy.
    """
    return _department_partition()[1]


def legacy_general_count() -> int | None:
    """Records in a pre-2026-10-26 kb_general, or None when there is none.

    Startup names a non-None answer, because those records are unreachable by
    construction (every spelling of "general" resolves to the global
    collection now) and left out of every listing - silent, they would just
    be lost. Moving them is the operator's call: re-ingest under "general" if
    everyone may read them, or under a department that is not a spelling of
    "general" if not."""
    for col in client.list_collections():
        if col.name == LEGACY_GENERAL_COLLECTION:
            return col.count()
    return None


def list_departments() -> list[str]:
    """Return the REAL departments: "general" plus every kb_* collection that
    actually holds documents.

    THE DEPARTMENT-LIST INVARIANT: a department list holds only real
    departments. Enumerating COLLECTIONS while delete_source removes DOCUMENTS
    is how an internal probe's empty leftovers can end up advertised on an
    admin surface - and _get_collection's get_or_create means even a delete or
    query naming a novel department mints an empty collection as a side
    effect. Empty collections are excluded here BY CONSTRUCTION, so no surface
    derived from this list - the /api/ingest/departments endpoint included -
    can advertise one, and the exclusion is logged loudly rather than silently
    applied.
    """
    real, residue = _department_partition()
    if residue:
        log.warning(
            "department-list invariant: excluded %d empty kb_* collection(s) "
            "(residue, not real departments): %s",
            len(residue), ", ".join(residue))
    return sorted(["general"] + real)


def count_documents(department: str | None = None) -> int:
    """Count total documents, optionally scoped to a department."""
    if department is not None:
        col = _existing_collection(department)
        return col.count() if col is not None else 0
    total = 0
    for col in client.list_collections():
        if col.name in (HELP_COLLECTION, LEGACY_GENERAL_COLLECTION):
            continue   # product help pages are not the operator's documents;
                       # a legacy kb_general is unreachable (its constant)
        try:
            total += col.count()
        except Exception:
            pass
    return total


def list_injection_flagged_sources() -> list[dict]:
    """Unique sources carrying injection_flagged chunks, with their trust
    tier - the admin visibility surface for the injection gate
    (tagged-not-quarantined content; quarantined content never reaches the
    index and lives in the quarantined_docs table instead)."""
    all_collection_names = [GLOBAL_COLLECTION] + [
        c.name for c in client.list_collections() if _is_corpus_collection(c.name)
    ]
    flagged: dict[tuple, dict] = {}
    for col_name in all_collection_names:
        dept_label = "general" if col_name == GLOBAL_COLLECTION else col_name[3:]
        try:
            col = client.get_or_create_collection(name=col_name, metadata=collection_metadata())
            results = col.get(include=["metadatas"])
            for meta in results["metadatas"]:
                if meta.get("injection_flagged") == "true":
                    source = meta.get("source", "unknown")
                    key = (source, dept_label)
                    if key not in flagged:
                        flagged[key] = {
                            "source": source,
                            "department": dept_label,
                            "trust": derive_trust(meta),
                            "injection_types": meta.get("injection_types", ""),
                        }
        except Exception:
            pass
    return list(flagged.values())


def list_pii_sources() -> list[dict]:
    """Return unique sources that were flagged during PII scanning."""
    all_collection_names = [GLOBAL_COLLECTION] + [
        c.name for c in client.list_collections() if _is_corpus_collection(c.name)
    ]
    flagged: dict[tuple, dict] = {}
    for col_name in all_collection_names:
        dept_label = "general" if col_name == GLOBAL_COLLECTION else col_name[3:]
        try:
            col = client.get_or_create_collection(name=col_name, metadata=collection_metadata())
            results = col.get(include=["metadatas"])
            for meta in results["metadatas"]:
                if meta.get("pii_flagged") == "true":
                    source = meta.get("source", "unknown")
                    key = (source, dept_label)
                    if key not in flagged:
                        flagged[key] = {
                            "source": source,
                            "department": dept_label,
                            "pii_types": meta.get("pii_types", ""),
                        }
        except Exception:
            pass
    return list(flagged.values())
