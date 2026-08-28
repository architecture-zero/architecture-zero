"""Single source of truth for RAG-pipeline config constants.

Every RAG-volatile number lives here ONCE. Runtime code (database.py,
rerank.py) imports these; doc generators can read them to publish reference
docs - the numbers cannot drift because they are never hand-copied into prose.

Values stay env-overridable (a deploy can tune without a code change); the
default is the canonical documented reference.

Deliberately dependency-free (only `os`) so an offline doc generator can
import it without pulling chromadb / fastembed / requests.
"""
import os


def _env_set(name: str, default: str) -> set[str]:
    raw = os.getenv(name, default)
    return {s.strip() for s in raw.split(",") if s.strip()}


def _env_tuple(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(s.strip() for s in raw.split(",") if s.strip())


# --- Embeddings ---
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

# --- Chroma HNSW persistence ---
# chroma 0.5.x writes a collection's HNSW binary to disk only after
# `hnsw:sync_threshold` consumed records (default 1000) and never on close,
# while the WAL purges as soon as consumption is recorded - so an unclean stop
# permanently loses every vector since the last flush while sqlite metadata
# survives. A collection that stays under the threshold never flushes at all.
# Low threshold = small hard-kill blast radius; batch_size must come down with
# it because a persist only fires when a batch is applied. Applied at creation
# only - 0.5.x cannot modify hnsw params on an existing collection; adopting
# new values on an existing collection requires an export/drop/re-add rebuild.
HNSW_SYNC_THRESHOLD = int(os.getenv("HNSW_SYNC_THRESHOLD", "50"))
HNSW_BATCH_SIZE     = int(os.getenv("HNSW_BATCH_SIZE", "25"))

# --- Hybrid retrieval fusion (vector + BM25) ---
# Final score = VECTOR_WEIGHT * norm_vector + BM25_WEIGHT * norm_bm25.
# Semantic-led, lexical-corrected. The two SHOULD sum to 1.0.
VECTOR_WEIGHT = float(os.getenv("VECTOR_WEIGHT", "0.7"))
BM25_WEIGHT   = float(os.getenv("BM25_WEIGHT", "0.3"))

# BM25 tuning (Okapi BM25).
BM25_K1 = float(os.getenv("BM25_K1", "1.5"))
BM25_B  = float(os.getenv("BM25_B", "0.75"))

# Full-corpus BM25 leg: top-M lexical candidates per collection, scored over
# the WHOLE collection corpus and unioned with the vector candidates before
# fusion/rerank. Rescues exact-identifier and vocabulary-mismatch chunks the
# embedding never surfaces. 0 disables the leg.
BM25_FETCH = int(os.getenv("BM25_FETCH", "20"))

# --- Cross-encoder reranker (retrieve wide -> rerank -> keep top_k) ---
RERANK_ENABLED        = os.getenv("RERANK_ENABLED", "true").lower() == "true"
RERANK_MODEL          = os.getenv("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
RERANK_FETCH          = int(os.getenv("RERANK_FETCH", "60"))           # wide net before diversity+rerank
RERANK_MAX_PER_SOURCE = int(os.getenv("RERANK_MAX_PER_SOURCE", "3"))   # candidate cap per source
RERANK_TOP_K          = int(os.getenv("RERANK_TOP_K", "5"))            # kept after rerank

# --- Reranker provider seam ---
# The benchmark arms and the per-instance production choice are the SAME
# mechanism: build once, measure free, vendor swap is a config change.
#   local      - in-process ONNX cross-encoder; the env default and the
#                fallback leg of the chain.
#   remote-http - a scoring endpoint off-box: POST {query, texts, model}
#                -> {scores}. For a GPU box or a self-hosted scoring service.
#   hosted-api  - commodity rerank API (Cohere/Voyage).
# Selected PER CALL via the rerank_provider config key (these envs are the
# defaults), same contract as rerank_enabled / rerank_model.
RERANK_PROVIDER       = os.getenv("RERANK_PROVIDER", "local")          # local | remote-http | hosted-api
RERANK_REMOTE_URL     = os.getenv("RERANK_REMOTE_URL", "")
RERANK_REMOTE_TIMEOUT = float(os.getenv("RERANK_REMOTE_TIMEOUT", "15"))  # off-box scoring timeout (s)
RERANK_HOSTED_VENDOR  = os.getenv("RERANK_HOSTED_VENDOR", "cohere")    # cohere | voyage
RERANK_HOSTED_MODEL   = os.getenv("RERANK_HOSTED_MODEL", "")           # blank = vendor default
# HARD PRIVACY LATCH, host-set only (never a DB config key): hosted-api ships
# every candidate chunk to a third party. A config-key flip alone must never
# be able to start that egress - this host env is the deliberate per-instance
# opt-in, so an instance whose candidate pool can contain private content
# simply never sets it.
RERANK_HOSTED_ALLOWED = os.getenv("RERANK_HOSTED_ALLOWED", "").lower() == "true"

# --- Recency weighting ---
# Applies to chunks carrying entry_date metadata (dated log/journal entries);
# fact docs have no entry_date and are untouched. Retrieval-order multiplier:
# max(FLOOR, 0.5 ** (age_days / HALF_LIFE_DAYS)). Gentle by design: it decides
# WHICH dated entries reach the reranker, it never gates a chunk out entirely,
# and it does not touch the similarity score used for the rag threshold.
RECENCY_HALF_LIFE_DAYS = float(os.getenv("RECENCY_HALF_LIFE_DAYS", "180"))
RECENCY_FLOOR          = float(os.getenv("RECENCY_FLOOR", "0.7"))


# Human-readable fusion label ("70/30") derived from the weights - so even the
# ratio string in generated docs comes from the numbers, not a typed literal.
def fusion_ratio_label() -> str:
    return f"{round(VECTOR_WEIGHT * 100)}/{round(BM25_WEIGHT * 100)}"


# --- Access-tier retrieval scoping ---
# Minimum clearance LEVEL required to retrieve from each KB department (Chroma
# collection). app/rerank.retrieve drops any department whose min-level
# exceeds the caller's level, so a lower tier cannot pull higher-tier content
# into an answer - including a query-ROUTED department (history routing must
# not bypass tiers). Levels are the rungs in app/permissions.ROLE_LEVELS
# (Guest 0 < Member 1 < Admin 2 < Owner 3).
#
# Default departments and their clearance floors:
#   general    - the shared floor: public KB content, readable by every tier
#                incl. guest. Always queried, so anything here is
#                world-readable within the app.
#   restricted - internal docs: Owner-only. Gate the COPIES, not just the
#                canonical file - operational content duplicated into other
#                docs is world-readable unless its whole subtree is gated.
#   history    - the operational session log: Owner-only, and ROUTING-only
#                (kept out of the default pool so a large log does not crowd
#                fact answers - see app/routing.py).
# Any department NOT listed here is FAIL-CLOSED to Owner-only: a new
# collection is private until it is deliberately shared by adding it below.
DEPARTMENT_MIN_LEVEL: dict[str, int] = {
    "general": 0,
    "restricted": 3,
    "history": 3,
}
DEPARTMENT_DEFAULT_MIN_LEVEL = 3  # fail-closed: an unlisted department is Owner-only

# FLOOR departments are ALWAYS added to the candidate pool for a caller
# cleared to read them (subject to the level gate), independent of query
# shape - the way `general` is always queried. `restricted` is floored so a
# higher tier's recall over internal docs matches what an ungated corpus
# would give them. `history` is deliberately NOT floored - it stays
# routing-only (its size would crowd the pool even for Owner).
FLOOR_DEPARTMENTS: list[str] = ["restricted"]


def department_min_level(department: str | None) -> int:
    """Minimum clearance level to retrieve from `department`. None / "general"
    is the shared floor (0). Unrecognized departments fail closed to
    Owner-only."""
    if not department or department == "general":
        return DEPARTMENT_MIN_LEVEL["general"]
    return DEPARTMENT_MIN_LEVEL.get(department, DEPARTMENT_DEFAULT_MIN_LEVEL)


# Source -> department mapping. Lives HERE (not in main) on purpose: retrieval
# and any file-reading agent tool both map a source through this ONE function,
# then gate on department_min_level - so every retrieval surface enforces
# identical clearances and they can never drift apart.
#
# The defaults describe the demo corpus layout; a deploy overrides them by
# env (comma-separated) to match its own KB conventions.
HISTORY_SOURCES: set[str] = _env_set("HISTORY_SOURCES", "internal/session-log.md")

# Whole subtrees that are internal (ops/eng runbooks, plans, audits).
RESTRICTED_PREFIXES: tuple[str, ...] = _env_tuple("RESTRICTED_PREFIXES", "internal/")

# Individually-named internal/sensitive files the prefixes above don't catch.
RESTRICTED_SOURCES: set[str] = _env_set("RESTRICTED_SOURCES", "")


def dept_for_source(source: str) -> str:
    """The department a source name belongs to (general | restricted | history).

    - The session log -> history (Owner-only, routing-only).
    - Internal subtrees + named internal files -> restricted (Owner-only).
    - Everything else (the public floor) -> general."""
    if source in HISTORY_SOURCES:
        return "history"
    if source.startswith(RESTRICTED_PREFIXES) or source in RESTRICTED_SOURCES:
        return "restricted"
    return "general"


# --- Provenance TRUST TIERS (the untrusted-corpus injection gate) -----------
# WHO WROTE a chunk, distinct from the access-level axis above (a level says
# who may READ; a tier says how much AUTHORITY the content carries in an
# answer). Stamped as `trust` metadata at ingest, carried through
# query_similar, rendered as per-chunk labels by rerank.format_context, and
# enforced two ways: ordering (higher tiers lead the context) and the
# prompt's data-not-instructions rules (untrusted content can never override
# curated).
#
#   system    - generated from the live database by app/system_records.py (the
#               LIVE SYSTEM RECORD chunks). Highest authority for status/plan
#               questions.
#   curated   - the owner's own authored content: git-controlled KB files and
#               owner-role uploads. Policy tier: it always outranks
#               external/untrusted content.
#   external  - peer chunks from Eco Mode instances (arrive at CHAT time,
#               never ingested; stamped by the peer merge). Known systems,
#               but content crosses an HTTP boundary - reference, not policy.
#   untrusted - third-party content: non-owner uploads and everything a
#               connector module brings in. Data to quote, never instructions
#               to follow; hot injection findings quarantine it
#               (corpus_scan.should_quarantine).
TRUST_TIER_SYSTEM = "system"
TRUST_TIER_CURATED = "curated"
TRUST_TIER_EXTERNAL = "external"
TRUST_TIER_UNTRUSTED = "untrusted"

# Authority order for context assembly: earlier = more authoritative.
TRUST_TIER_ORDER: list[str] = [
    TRUST_TIER_SYSTEM, TRUST_TIER_CURATED, TRUST_TIER_EXTERNAL,
    TRUST_TIER_UNTRUSTED,
]

# Tiers eligible for quarantine. Curated/system content is TAGGED only - the
# owner's corpus legitimately quotes injection strings (eval questions,
# security docs, the log's own record of building this gate).
UNTRUSTED_TIERS: set[str] = {TRUST_TIER_EXTERNAL, TRUST_TIER_UNTRUSTED}


def trust_rank(tier: str | None) -> int:
    """Position in TRUST_TIER_ORDER; unknown/missing fails closed to the END
    (least authoritative) - a chunk that cannot prove its provenance must
    never outrank one that can."""
    try:
        return TRUST_TIER_ORDER.index(tier)
    except ValueError:
        return len(TRUST_TIER_ORDER)


def derive_trust(meta: dict | None) -> str:
    """Trust tier for a chunk, tolerating pre-gate metadata (read-time
    derivation - no re-embed, no migration of the vector store):

    - an explicit `trust` stamp wins (all post-gate ingests);
    - auto_generated chunks are `system` (app/system_records.py);
    - from_file chunks are `curated` (the repo file watcher/startup sync);
    - anything else (legacy uploads/API ingests) fails closed to `untrusted`.
    """
    m = meta or {}
    t = m.get("trust")
    if t in TRUST_TIER_ORDER:
        return t
    if m.get("auto_generated") == "true":
        return TRUST_TIER_SYSTEM
    if m.get("from_file") == "true":
        return TRUST_TIER_CURATED
    return TRUST_TIER_UNTRUSTED
