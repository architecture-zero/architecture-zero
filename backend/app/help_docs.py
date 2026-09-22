"""In-product help: the assistant answers questions about ITSELF.

Ported from the upstream product surface on 2026-09-21. The help pages in
backend/app/help/*.md ship INSIDE the image and are synced into their own
reserved collection at boot, so every instance shape gets them: a stock
compose deployment, a box with no internet, an operator who replaced the
starter knowledge folder with their own documents on day one.

Why a reserved collection and not the corpus. The starter knowledge folder
already lets a fresh instance explain itself in normal chat (the README's
"built-in onboarding assistant"), and that stays. But it is the OPERATOR's
corpus: they are expected to replace it, and once they do, nothing answers
"how do I sign in" any more. Product help has to survive that, and it has
to stay out of the operator's way while it does:
  - it must never compete with their documents in a normal answer, never
    show in their Knowledge Base tab, never count in their document total,
    never move their eval corpus fingerprint. database._is_corpus_collection()
    is the one predicate every corpus-wide enumeration uses, and it excludes
    this collection.
  - a normal question must never be answered from a help page, and a help
    question must never be answered from the operator's documents. The chat
    route's help lane reads THIS collection only (query_similar
    only_department=True); the global collection is not merged in.
  - the name is reserved: every write door refuses department="help", so a
    manage_kb user cannot plant a document the Help button would then cite
    as the product's own guidance, and no account can LIVE in the department
    (its normal answers would merge the help pages in).

Sync is idempotent and cheap: a state file holds each page's sha256 and chunk
count; an unchanged page costs nothing at boot, a changed page is re-embedded
(its old chunks deleted first, so a shorter page leaves no stale tail), and a
page removed from the image is removed from the collection. THE STORE IS THE
WITNESS, the state file only a cache: chroma 0.5.x persists a small
collection's vectors only at the shutdown flush, so an unclean stop loses them
while the metadata survives - a sync that trusted its own state would then
report "unchanged" and every help question would be refused until a page's
bytes changed. So a page counts as unchanged only when its sha matches AND the
store holds the chunk count the state recorded, and a sync that wrote flushes
the vector segments before it returns. Chunks are one per `## ` section,
heading kept with its body, so a help answer cites the section that answers.
"""
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from fastapi import HTTPException

from app import database
from app.rag_config import TRUST_TIER_CURATED

log = logging.getLogger("help_docs")

HELP_DEPARTMENT = database.HELP_DEPARTMENT
HELP_DIR = Path(__file__).resolve().parent / "help"
SOURCE_PREFIX = "help/"
_STATE_NAME = "help_sync_state.json"
_H2 = re.compile(r"^## ", re.MULTILINE)
# A help section is short; an oversized one splits with its heading re-carried.
_CHUNK = 1200
_OVERLAP = 150


def _state_path() -> Path:
    return Path(os.getenv("HELP_SYNC_STATE_DIR",
                          os.getenv("DATA_DIR", "/app/data"))) / _STATE_NAME


def pages() -> list[Path]:
    if not HELP_DIR.is_dir():
        return []
    return sorted(p for p in HELP_DIR.glob("*.md") if p.is_file())


def page_name(path: Path) -> str:
    return f"{SOURCE_PREFIX}{path.name}"


def read_page(name: str) -> str | None:
    """The page's text for the citation viewer, MEMBERSHIP-GATED: `name` must
    equal one of pages()' names exactly. Resolved by comparing against the
    listing, never by joining the caller's string onto a path, so
    `help/../main.py` is simply not a page."""
    for p in pages():
        if page_name(p) == name:
            return p.read_text(encoding="utf-8", errors="ignore")
    return None


def refuse_reserved_department(department: str | None) -> None:
    """Every operator-facing write door calls this. The help collection is
    product-owned: its pages come from the image, and the Help button cites
    them as the product's own guidance. A manage_kb user must not be able to
    plant a document there, delete the pages (they would return at the next
    boot anyway), or place an account in the department - an account whose
    department is "help" would have the help pages merged into its NORMAL
    answers, because a department resolves through the same machinery."""
    if (department or "").strip().lower() == HELP_DEPARTMENT:
        raise HTTPException(
            status_code=400,
            detail=f"'{HELP_DEPARTMENT}' is reserved for the product's own help "
                   "pages; choose another department.")


def _plain(text: str) -> list[str]:
    out, start = [], 0
    while start < len(text):
        out.append(text[start:start + _CHUNK])
        if start + _CHUNK >= len(text):
            break
        start += _CHUNK - _OVERLAP
    return out


def chunk_sections(text: str) -> list[str]:
    """One chunk per '## ' section, heading kept with its body. The preamble
    (the page title and its intro) is its own chunk; an oversized section is
    split with the heading re-carried on every piece."""
    starts = [m.start() for m in _H2.finditer(text)]
    if not starts:
        return _plain(text.strip()) if text.strip() else []
    out: list[str] = []
    pre = text[:starts[0]].strip()
    if pre:
        out.extend(_plain(pre))
    for s, e in zip(starts, starts[1:] + [len(text)]):
        section = text[s:e].strip()
        if not section:
            continue
        if len(section) <= _CHUNK + _OVERLAP:
            out.append(section)
            continue
        heading, _, body = section.partition("\n")
        out.extend(f"{heading}\n{piece}" for piece in _plain(body))
    return out


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    except Exception as e:
        # A lost state file costs one re-embed at the next boot, nothing else.
        log.warning("help_docs: state not saved (%s) - every page re-syncs next boot", e)


def _store_counts() -> dict[str, int]:
    """Chunks per help page AS THE STORE HOLDS THEM - the witness the state
    file is checked against. Unreadable store = empty = every page re-syncs,
    which is the safe direction."""
    try:
        return {r["source"]: int(r["count"])
                for r in database.list_sources(department=HELP_DEPARTMENT)}
    except Exception as e:
        log.warning("help_docs: store unreadable (%s) - every page re-syncs", e)
        return {}


def sync(force: bool = False) -> dict:
    """Bring the help collection up to date with the pages in the image.
    Returns counts for the boot log: pages, ingested, unchanged, removed, chunks."""
    state = _load_state()
    store = _store_counts()
    seen: dict[str, dict] = {}
    ingested = unchanged = chunks = 0
    for path in pages():
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        name = page_name(path)
        prev = state.get(name) if isinstance(state.get(name), dict) else {}
        if (not force and prev.get("sha") == sha
                and store.get(name, 0) == prev.get("chunks", -1)):
            seen[name] = prev
            unchanged += 1
            continue
        pieces = chunk_sections(raw.decode("utf-8", errors="ignore").strip())
        # Old chunks first: a rewritten page with fewer sections must not leave
        # its former tail behind under the same source name.
        database.delete_source(name, department=HELP_DEPARTMENT)
        seen[name] = {"sha": sha, "chunks": len(pieces)}
        if not pieces:
            continue
        # Curated tier: the injection gate TAGS a finding in a curated chunk
        # and never withholds it (a help page legitimately describes the very
        # attack strings the scan looks for). The gate itself still runs -
        # add_documents_batch is the one choke point.
        entries = [(f"{name}::chunk_{i}", piece,
                    {"source": name, "chunk": i, "total_chunks": len(pieces),
                     "department": HELP_DEPARTMENT, "trust": TRUST_TIER_CURATED,
                     "from_file": "true", "help_page": "true"})
                   for i, piece in enumerate(pieces)]
        database.add_documents_batch(entries, department=HELP_DEPARTMENT)
        ingested += 1
        chunks += len(pieces)
    removed = 0
    for stale in sorted(set(state) - set(seen)):
        database.delete_source(stale, department=HELP_DEPARTMENT)
        removed += 1
    if ingested or removed:
        # Persist the vectors NOW rather than at the next graceful stop - the
        # loss class this module's docstring names.
        try:
            database.flush_vector_segments()
        except Exception as e:
            log.warning("help_docs: vector flush failed (%s) - the next graceful stop flushes", e)
    _save_state(seen)
    return {"pages": len(seen), "ingested": ingested, "unchanged": unchanged,
            "removed": removed, "chunks": chunks}
