"""Live-system records: the producer for the `system` trust tier.

The tier shipped complete and empty. Ranking, labelling, quarantine exemption
and the grounding rules that tell the model a `[LIVE SYSTEM RECORD]` block wins
on status questions were all in place, with nothing writing to it - so the
assistant answered "is the injection scan on here?" from prose someone wrote
once, or not at all. This generates a small set of records from the instance's
own live state and re-ingests them, so those answers come from the deployment
rather than from documentation about deployments in general.

WHAT MAKES THIS DIFFERENT FROM ORDINARY INGEST, and why the rules below are
not negotiable: system-tier content LEADS the assembled context, is lifted to
the front of the pool on status-shaped queries, is labelled to the model as
current truth, and is EXEMPT FROM THE QUARANTINE SCAN. It is the only content
class with all four properties at once. A wrong record is not a bad search
result - it is an authoritative wrong answer, and a record carrying attacker
text would be a prompt injection the gate is designed not to catch.

THE SAFETY RULE. A value reaches a record only if it is a number, a bool, a
code-produced date, or a string that survives an ALLOWLIST - either membership
in a set built from repo constants (`_safe`) or a strict character-and-length
shape (`_safe_token`). No free-text database column is ever interpolated. This
is deliberately a rule about the CLASS of value rather than a list of forbidden
fields: a blocklist silently admits every column added later, an allowlist
fails closed. `_redacted` is the single placeholder, so a failure is visible in
the record rather than silent.

Second layer, because the first one is code that can be wrong: the composed
text is run through the injection scan before it is written, and any finding
ABORTS the write. `should_quarantine` returns False for this tier before it
looks at severity, so nothing downstream would stop it. A finding here means
this module is broken, not that the corpus is quoting something.

WHAT IS DELIBERATELY NOT HERE: per-source listings (source names are
caller-supplied upload filenames), anything from the audit log (it stores a
prefix of real user prompts), chat sessions and messages (user-authored, and
retrieval has no tenant filter, so embedding any of it would de-scope it
permanently), quarantined text (that IS the captured attack payload), and eval
question or answer text (the grading key, and injection-cohort rows carry the
planted poison).

EXPOSURE AN OPERATOR SHOULD KNOW: these land in `restricted`, which is Owner
clearance in-app - but `restricted` is also served to an `all`-scoped peer key
when peer serving is enabled. That is the same exposure the other `internal/`
documents already carry; it is stated here because a live posture record
travelling off-box is a more surprising consequence than a runbook doing so.
"""
import hashlib
import re
from datetime import datetime, timezone

from app.chunking import chunk_markdown_sections
from app.database import (add_documents_batch, delete_documents, get_source_ids,
                          list_departments, count_documents, list_sources,
                          corpus_fingerprint, list_injection_flagged_sources)
from app.logger import log, log_error

# Owner clearance, and a FLOOR department - always added to a cleared caller's
# candidate pool regardless of query shape, which is what a status record needs
# (a chunk cannot be lifted to the front if it never entered the pool).
# Deliberately not `general`: that floor is world-readable within the app, and a
# world-readable status record would put two prompt blocks in direct conflict -
# the non-owner rules forbid recounting internal metrics even when they appear
# in context, while the grounding rules say a live system record wins.
DEPARTMENT = "restricted"

# Every generated source starts here. The `internal/` prefix is what maps these
# to DEPARTMENT through dept_for_source, so the retrieval gate and the agent's
# file-tool gate resolve the same string the same way. Extensionless on purpose:
# these are not files, and a name ending in a watched extension could collide
# with one. Never prefixed `docs/` - the orphan pruner deletes any `docs/`
# source with no backing file on disk, and these have none by design.
NAMESPACE = "internal/system/"

POSTURE_SOURCE = NAMESPACE + "posture"
CORPUS_SOURCE = NAMESPACE + "corpus"
MEASUREMENT_SOURCE = NAMESPACE + "measurement"

# The authority for what this module emits. Prose never states a count of these
# - the tuple is the count, and a number written in a sentence drifts.
SOURCES = (POSTURE_SOURCE, CORPUS_SOURCE, MEASUREMENT_SOURCE)

_REDACTED = "(withheld)"

# A token shape for identifiers an operator configures (model ids, provider
# names). These are not free user text, but they are not drawn from a fixed set
# either, so membership cannot gate them - the shape does. Anything with
# whitespace, punctuation beyond the few characters an identifier needs, or
# length past the cap is withheld rather than rendered.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")

# The corpus fingerprint is code-composed - counts and a hex digest - so it is
# safe by construction, but its punctuation fails the identifier shape above.
# It gets its OWN exact-shape check rather than a looser general rule: widening
# _TOKEN_RE to admit one known-good value would admit every value that happens
# to share its punctuation.
_FINGERPRINT_RE = re.compile(r"^src=[0-9]+;chunks=[0-9]+;sha=[0-9a-f]+$")


def _safe(value, allowed) -> str:
    """Render `value` only if it is a member of `allowed`, else the placeholder.

    `allowed` is built at the call site from repo constants, so the set of
    renderable strings is fixed by the code rather than by the database.
    """
    return str(value) if value in allowed else _REDACTED


def _safe_token(value) -> str:
    """Render an operator-configured identifier only if it has identifier shape."""
    if isinstance(value, str) and _TOKEN_RE.match(value):
        return value
    return _REDACTED


def _safe_fingerprint(value) -> str:
    """Render the corpus fingerprint only in its exact composed shape.

    corpus_fingerprint() returns "unavailable:<ExceptionName>" when it cannot
    read the index. That is a real and useful answer, so it is rendered as a
    plain statement rather than as a withheld value - a record that hides the
    difference between "no corpus" and "could not read the corpus" is worse
    than one that says so.
    """
    if isinstance(value, str):
        if _FINGERPRINT_RE.match(value):
            return value
        if value.startswith("unavailable:"):
            return "unavailable (the index could not be read at generation time)"
    return _REDACTED


def _num(value) -> str:
    """Render an integer or float. Anything else is a bug upstream, not content."""
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else _REDACTED


def _yesno(value) -> str:
    return "yes" if bool(value) else "no"


def _today() -> str:
    """Date granularity, never a timestamp.

    The freshness line is part of the chunk text, so its granularity decides how
    often every record re-embeds. A timestamp would re-embed all of it on every
    boot; a date means same-day restarts produce byte-identical text and embed
    nothing at all.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# -- Snapshot -----------------------------------------------------------------
# ONE read of live state, taken before anything is written. The corpus record
# counts the corpus, and the producer writes to the corpus - so reading per
# record would let one record's write perturb another's numbers, which perturbs
# the first one's next generation, and the pair never converges. One snapshot,
# plus the self-exclusion below, makes the output a fixed point.

def _snapshot() -> dict:
    import os
    from app.config import get_config
    from app.security import get_security_config
    from app.agent import get_tool_config
    from app.providers import get_provider_config
    from app.corpus_scan import INJECTION_SCAN_MODE
    from app.rerank import rerank_enabled, rerank_provider, rerank_model
    from app.runtime_config import (ALLOW_GUEST_MODE, RAG_ONLY_MODE, PII_SCAN_MODE,
                                    DEFAULT_MODEL, DEMO_DAILY_GUEST_LIMIT,
                                    ENCRYPTION_AT_REST_VERIFIED)
    from app.routers.chat import GUEST_MAX_TURNS
    from app.users import list_users, owner_exists
    from app.trust_panel import derive_trust_panel
    from app.auth import ENABLE_AUTH

    snap: dict = {}

    # Posture: constants, env flags, and an EXPLICIT config key allowlist.
    # Never get_all_config() - that store is an untyped operator key/value table
    # and provider API keys live in it.
    snap["auth_enabled"] = ENABLE_AUTH
    snap["guest_env"] = ALLOW_GUEST_MODE
    snap["guest_config"] = get_config("guest_mode_enabled", "false") == "true"
    snap["guest_daily_limit"] = DEMO_DAILY_GUEST_LIMIT
    snap["guest_max_turns"] = GUEST_MAX_TURNS
    snap["rag_only"] = RAG_ONLY_MODE
    snap["pii_scan_mode"] = PII_SCAN_MODE
    snap["injection_scan_mode"] = INJECTION_SCAN_MODE
    snap["encryption_verified"] = ENCRYPTION_AT_REST_VERIFIED
    snap["security"] = get_security_config()
    snap["tools"] = get_tool_config()
    snap["provider"] = get_provider_config()
    snap["default_model"] = get_config("chat_model", "") or DEFAULT_MODEL
    snap["rerank_enabled"] = rerank_enabled()
    snap["rerank_provider"] = rerank_provider()
    snap["rerank_model"] = rerank_model()
    snap["build"] = os.getenv("GIT_SHA", "")

    # Accounts: existence and per-role counts. Never a username - the roster is
    # an operator surface, not corpus content.
    snap["owner_exists"] = owner_exists()
    roles: dict[str, int] = {}
    active = 0
    for u in list_users():
        if not u.get("is_active", True):
            continue
        active += 1
        role = u.get("role", "member")
        roles[role] = roles.get(role, 0) + 1
    snap["accounts_active"] = active
    snap["accounts_by_role"] = roles

    # Corpus: read before any write, and self-excluding. A source in this
    # module's own namespace is not corpus knowledge, it is bookkeeping about
    # the corpus, and counting it would make the record describe itself.
    snap["departments"] = list_departments()
    snap["fingerprint"] = corpus_fingerprint()
    own_chunks = 0
    foreign_sources = 0
    for s in list_sources():
        if str(s.get("source", "")).startswith(NAMESPACE):
            own_chunks += s.get("count", 0)
        else:
            foreign_sources += 1
    snap["source_count"] = foreign_sources
    snap["own_chunks"] = own_chunks
    snap["chunk_total"] = count_documents()
    snap["flagged_count"] = len(list_injection_flagged_sources())

    # Measurement: the panel already materialises plain scalars and already
    # refuses honestly when nothing qualifies. Re-walking eval rows here would
    # put question text and captured context within reach of a bug.
    snap["panel"] = derive_trust_panel(admin=False)
    return snap


# -- Record builders ----------------------------------------------------------

def build_posture(snap: dict) -> str:
    from app.permissions import ROLE_LEVELS
    scan_modes = {"off", "tag", "quarantine"}
    pii_modes = {"off", "flag", "redact"}
    sec = snap["security"] if isinstance(snap.get("security"), dict) else {}
    guest_open = bool(snap["guest_env"]) and bool(snap["guest_config"])

    lines = [
        "# Live instance posture",
        "",
        "Generated from this deployment's own configuration on " + _today()
        + " (UTC). It describes THIS instance, not the defaults in the docs.",
        "",
        "## Access",
        "",
        "- Authentication required: " + _yesno(snap["auth_enabled"]),
        "- Guest (anonymous) chat open: " + _yesno(guest_open)
        + " (it needs both the environment opt-in and the admin toggle; the env "
        "half is " + _yesno(snap["guest_env"]) + " and the admin half is "
        + _yesno(snap["guest_config"]) + ")",
        "- Guest turns allowed per conversation: " + _num(snap["guest_max_turns"]),
        "- Global daily guest request budget: "
        + (_num(snap["guest_daily_limit"]) if snap["guest_daily_limit"] else "off"),
        "- An Owner account exists: " + _yesno(snap["owner_exists"]),
        "- Active accounts: " + _num(snap["accounts_active"]),
    ]
    for role in sorted(ROLE_LEVELS):
        if role in snap["accounts_by_role"]:
            lines.append("- Active accounts with role "
                         + _safe(role, set(ROLE_LEVELS)) + ": "
                         + _num(snap["accounts_by_role"][role]))

    lines += [
        "",
        "## Content gates",
        "",
        "- Corpus injection scan: " + _safe(snap["injection_scan_mode"], scan_modes)
        + " (off indexes everything, tag marks and keeps, quarantine withholds "
        "for review)",
        "- Prompt injection screening: " + _yesno(sec.get("injection_protection")),
        "- PII scanning: " + _safe(snap["pii_scan_mode"], pii_modes),
        "- Indexed sources currently flagged by the injection scan: "
        + _num(snap["flagged_count"]),
        "- Retrieval-only mode (never answer from model memory): "
        + _yesno(snap["rag_only"]),
        "",
        "## Limits",
        "",
        "- Per-IP rate limiting: " + _yesno(sec.get("rate_limit_enabled"))
        + (" (" + _num(sec.get("rate_limit_requests")) + " requests per "
           + _num(sec.get("rate_limit_window")) + " seconds)"
           if sec.get("rate_limit_enabled") else ""),
        "- Encryption at rest confirmed by the operator: "
        + _yesno(snap["encryption_verified"]),
        "",
        "## Answering",
        "",
        "- Default answering model: " + _safe_token(snap["default_model"]),
        "- Reranker enabled: " + _yesno(snap["rerank_enabled"])
        + (" (" + _safe_token(snap["rerank_provider"]) + ", model "
           + _safe_token(snap["rerank_model"]) + ")"
           if snap["rerank_enabled"] else ""),
    ]
    if snap["build"]:
        lines += ["", "## Build", "",
                  "- Running build: " + _safe_token(snap["build"])]
    return "\n".join(lines) + "\n"


def build_corpus(snap: dict) -> str:
    from app.rag_config import DEPARTMENT_MIN_LEVEL
    known = set(DEPARTMENT_MIN_LEVEL)
    named = [d for d in snap["departments"] if d in known]
    unnamed = len([d for d in snap["departments"] if d not in known])

    lines = [
        "# Live corpus state",
        "",
        "Generated from this deployment's own index on " + _today() + " (UTC).",
        "",
        "## Size",
        "",
        "- Indexed sources: " + _num(snap["source_count"]),
        "- Indexed chunks: " + _num(snap["chunk_total"]),
        "- Corpus fingerprint: " + _safe_fingerprint(snap["fingerprint"]),
        "",
        "The source count EXCLUDES the generated records themselves. They are "
        "bookkeeping about the corpus rather than corpus knowledge, and counting "
        "them would make this record describe its own size and never settle.",
        "",
        "## Departments",
        "",
        "- Departments holding content: "
        + (", ".join(_safe(d, known) for d in named) if named else "none"),
    ]
    if unnamed:
        lines.append("- Further departments configured on this instance: "
                     + _num(unnamed)
                     + " (named on the operator surfaces, not here)")
    lines += [
        "",
        "A department is an access tier rather than a folder: retrieval refuses "
        "any department whose minimum clearance is above the caller's own, so a "
        "lower tier cannot pull higher-tier content into an answer.",
    ]
    return "\n".join(lines) + "\n"


def build_measurement(snap: dict) -> str:
    panel = snap["panel"] if isinstance(snap.get("panel"), dict) else {}
    lines = [
        "# Live measurement state",
        "",
        "Generated from this deployment's own evaluation runs on " + _today()
        + " (UTC).",
        "",
    ]
    if not panel.get("available"):
        lines.append(
            "No evaluation run on this instance qualifies as a complete "
            "measurement yet, so there is no score to report. That is a fact "
            "about this deployment and not about the evaluation system: the "
            "harness ships and the question set is seeded, but nothing has "
            "produced a complete run here.")
        return "\n".join(lines) + "\n"

    def band(key, label):
        b = panel.get(key)
        if not isinstance(b, dict):
            return None
        return ("- " + label + ": " + _num(b.get("low")) + " to "
                + _num(b.get("high")) + " percent across "
                + _num(b.get("runs")) + " comparable run(s)")

    lines += ["## Scores", ""]
    for key, label in (("correctness", "Answer correctness"),
                       ("holdout", "Held-out question correctness"),
                       ("gap", "Tuned-to-holdout gap"),
                       ("faithfulness", "Faithfulness to retrieved context"),
                       ("freshness", "Freshness")):
        row = band(key, label)
        if row:
            lines.append(row)
    hon = panel.get("honesty")
    if isinstance(hon, dict) and hon.get("pct") is not None:
        lines.append("- Honesty (declining to answer what is not in the corpus): "
                     + _num(hon.get("pct")) + " percent over "
                     + _num(hon.get("n")) + " question(s)")
    ret = panel.get("retrieval")
    if isinstance(ret, dict) and ret.get("pct") is not None:
        lines.append("- Retrieval hit rate: " + _num(ret.get("pct")) + " percent")

    lines += [
        "",
        "## Comparability",
        "",
        "- Last measured: " + _safe_token(panel.get("measured_at")),
        "- Corpus at measurement time: "
        + _safe_token(panel.get("corpus_fingerprint_short")),
        "",
        "A range rather than one number is deliberate. Runs are grouped only "
        "when the writer model, the corpus fingerprint, the judging instrument "
        "and the question count all match, so the spread is run-to-run variance "
        "and not drift between measurements that were never comparable.",
    ]
    return "\n".join(lines) + "\n"


# -- The write path -----------------------------------------------------------

def _ingest(source: str, text: str) -> dict:
    """Content-addressed delta write for one record.

    Modelled on the knowledge-file sync rather than on any per-document path:
    the chunk id is an md5 of (department, source, chunk TEXT), so an unchanged
    record embeds nothing at all. Keying an id on chunk POSITION instead is the
    trap here - a position is stable across content changes, so it is useless as
    a change signal and every boot pays a full re-embed of every record.
    """
    from app.corpus_scan import scan

    # The write path will NOT stop a bad record: the quarantine decision returns
    # False for this tier before it looks at severity. So the producer screens
    # its own output, and a finding aborts rather than tags - a hit here means
    # this module composed something it should not have, which is a bug to fix
    # and not content to label.
    findings = scan(text)
    if findings:
        log_error("system_record_self_scan_failed", source=source,
                  findings=len(findings))
        return {"status": "error", "error": "self-scan found injection-shaped text"}

    chunks = chunk_markdown_sections(text)
    desired: dict[str, tuple[int, str]] = {}
    for i, chunk in enumerate(chunks):
        doc_id = hashlib.md5(
            (DEPARTMENT + "::" + source + "::" + chunk).encode(),
            usedforsecurity=False).hexdigest()
        desired[doc_id] = (i, chunk)

    # The SAME department literal on both sides. Reading existing ids under one
    # spelling and writing under another returns an empty existing-set forever,
    # which silently turns the delta back into a full re-embed.
    existing = set(get_source_ids(source, DEPARTMENT))
    stale = existing - desired.keys()

    new_entries = []
    for doc_id, (i, chunk) in desired.items():
        if doc_id in existing:
            continue
        # BOTH keys, because they drive different machinery: the tier derives
        # from `trust`, while the status lift and the [LIVE SYSTEM RECORD] label
        # key on `auto_generated`. Setting one and not the other yields a record
        # that is either unranked or unlabelled.
        meta = {"source": source, "chunk": i,
                "auto_generated": "true", "trust": "system"}
        new_entries.append((doc_id, chunk, meta))

    # ADD FIRST, PRUNE LAST - the order the file sync settled on. For a changed
    # record the stale set is the previous text of the chunks that moved, so
    # pruning first would leave the record with neither generation indexed if
    # the embed fails in between. Content-addressed ids make this order safe:
    # the worst case is both generations present for one batch.
    added = add_documents_batch(new_entries, department=DEPARTMENT) if new_entries else 0
    if stale:
        delete_documents(sorted(stale), DEPARTMENT)

    log("system_record_sync", source=source, chunks=len(chunks), added=added,
        removed=len(stale), unchanged=len(desired) - added)
    return {"status": "ok", "chunks": len(chunks), "added": added,
            "removed": len(stale)}


def sync_system_records() -> dict:
    """Regenerate every live-system record. Safe to call repeatedly.

    Per-record isolation: one builder raising must not cost the others their
    refresh, because a stale record that still LOOKS current is worse than a
    missing one at a tier the model is told to trust.
    """
    try:
        snap = _snapshot()
    except Exception as e:
        log_error("system_records_snapshot_failed", error=str(e))
        return {"snapshot": {"status": "error", "error": str(e)}}

    out: dict = {}
    for source, builder in ((POSTURE_SOURCE, build_posture),
                            (CORPUS_SOURCE, build_corpus),
                            (MEASUREMENT_SOURCE, build_measurement)):
        try:
            out[source] = _ingest(source, builder(snap))
        except Exception as e:
            log_error("system_record_failed", source=source, error=str(e))
            out[source] = {"status": "error", "error": str(e)}
    return out
