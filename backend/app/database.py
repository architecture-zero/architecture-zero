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
    HNSW_SYNC_THRESHOLD, HNSW_BATCH_SIZE,
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

    None / "general" -> global knowledge_base collection.
    Any other value  -> kb_<department> collection (created on first use).
    """
    return client.get_or_create_collection(
        name=_collection_name(department), metadata=collection_metadata())


def _collection_name(department: str | None = None) -> str:
    """The collection name for a department - ONE derivation, shared by the
    get-or-create path and the does-it-exist path so they cannot disagree."""
    if not department or department == "general":
        return GLOBAL_COLLECTION
    # Sanitize: lowercase, replace spaces/special chars with underscore
    safe = re.sub(r"[^a-z0-9_-]", "_", department.lower())
    return f"kb_{safe}"


def _existing_collection(department: str | None = None):
    """The department's collection, or None if it does not exist yet.

    The delete-side counterpart to _get_collection, which is get_or_create and
    therefore MINTS an empty collection as a side effect of being asked about
    one. That side effect is how ORDINARY code invented departments, not just
    probes: kb_autogen's generators call delete_source on their no-data path
    ("if not rows: delete_source('health-current', 'health')"), so a module
    with nothing to say minted an empty kb_health at every boot, which
    list_departments() then advertised as a real department.

    Found 2026-08-26 by the department-list invariant's FIRST boot report on
    the live instance, which named dj, health, schedule and tasks - none of
    them probe residue. Without this, that report would fire at ERROR on every
    boot for a benign condition, and a guard that cries wolf every boot is one
    nobody reads.

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
    # access, so a second path would quietly drop those stubs (the 2026-08-26
    # BM25 test caught exactly that). get_or_create on a collection already
    # proven to exist creates nothing.
    return _get_collection(department)


def _embed(text: str, retries: int = 2) -> list[float]:
    """Embed with retry + backoff. One transient slow/failed embedding call
    must not abort a whole-file ingest: a big file rolls the dice hundreds of
    times, so big files die first without the retry. Timeout is per-attempt
    and sized for a CPU-contended box, not the idle-case latency."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                f"{EMBED_BASE}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": text},
                timeout=60
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
    avg_dl = sum(len(d) for d in tokenized_docs) / max(len(tokenized_docs), 1)
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
         # DB-truth autogen chunks (the autogen sync stamps auto_generated on
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
    return meta


def add_document(doc_id: str, text: str, metadata: dict, department: str | None = None,
                 quarantine_exempt: bool = False):
    """Index one chunk - and run the UNTRUSTED-CORPUS INJECTION GATE.

    The gate itself lives in _gate_chunk, shared by BOTH write shapes: single
    writes land here (file watcher, autogen sync, ingest/upload endpoints)
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
    col.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[meta]
    )
    _invalidate_lexical_index(col.name)


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
        col.upsert(ids=ids[sl], embeddings=embeddings,
                   documents=texts[sl], metadatas=metas[sl])
    _invalidate_lexical_index(col.name)
    return len(ids)


def query_similar(query: str, n_results: int = 5,
                  department: str | list[str] | None = None) -> list[dict]:
    """Hybrid vector + BM25 search. Merges the global KB with each department
    KB given. Accepts a single department or a list (e.g. the user's own
    department plus a query-routed one - see app/routing.py); the global
    collection is always queried."""
    embedding = _embed(query)
    fetch_k = max(n_results * 2, 10)

    all_docs: list[str] = []
    all_distances: list[float] = []
    all_metas: list[dict] = []

    departments = [department] if isinstance(department, str) else list(department or [])
    collections_to_query = [_get_collection()]  # always include global
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
            k = min(fetch_k, count)
            while True:
                try:
                    results = col.query(
                        query_embeddings=[embedding],
                        n_results=k,
                        include=["documents", "distances", "metadatas"]
                    )
                    break
                except Exception as e:
                    # hnswlib refuses a knn query whose k approaches the
                    # index's element count ("Cannot return the results in a
                    # contigious 2D array" - typo verbatim): a small HNSW
                    # graph is not guaranteed traversable to every node.
                    # Halve k and retry rather than losing the collection's
                    # whole vector leg (and, via the continue below, its BM25
                    # leg) - a silent per-collection loss that reads as "no
                    # knowledge found".
                    if k <= 1 or "contigious 2D array" not in str(e):
                        raise
                    log.warning("query_similar: %s knn refused k=%d (%s), retrying k=%d",
                                getattr(col, "name", "?"), k, e, k // 2)
                    k //= 2
            vector_ids = results["ids"][0] if results.get("ids") else []
            all_docs.extend(results["documents"][0] if results["documents"] else [])
            all_distances.extend(results["distances"][0] if results["distances"] else [])
            all_metas.extend(results["metadatas"][0] if results["metadatas"] else [])
        except Exception as e:
            # Don't hide infrastructure failures behind an empty result - a
            # down/corrupt Chroma otherwise reads as "no knowledge found" in
            # chat.
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
        c.name for c in client.list_collections()
        if c.name.startswith("kb_") and c.name != GLOBAL_COLLECTION
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


def get_source_ids(source: str, department: str | None = None) -> list[str]:
    """All chunk ids currently indexed for a source. The read half of delta
    ingestion: ids are content-addressed, so comparing this set against the
    desired set yields exactly what to embed and what to drop."""
    col = _existing_collection(department)
    if col is None:
        return []
    return list(col.get(where={"source": source}).get("ids") or [])


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
        if not col.name.startswith("kb_") or col.name == GLOBAL_COLLECTION:
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
        c.name for c in client.list_collections()
        if c.name.startswith("kb_") and c.name != GLOBAL_COLLECTION
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
        c.name for c in client.list_collections()
        if c.name.startswith("kb_") and c.name != GLOBAL_COLLECTION
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
