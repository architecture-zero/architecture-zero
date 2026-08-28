"""Shared chunking for KB ingestion.

One chunker for every ingestion call site (file sync/watcher, upload, autogen
sync) - a fixed character loop copy-pasted per call site is how chunking
drifts.

chunk_plain: the plain character chunker.

chunk_dated_markdown: structure-aware mode for the session log. A chunk never
straddles two '## ' entries, oversized entries are sub-chunked with the entry
heading prefixed to every piece (each chunk carries its own date + title
context instead of a blind char-offset slice), and the entry date parsed from
the heading is returned so ingestion can stamp entry_date metadata - which is
what recency weighting keys on (database._recency_multiplier).

chunk_markdown_sections: the same section algorithm for general markdown,
dates ignored. Blind fixed-size windows dilute atomic facts below the
retrieval pool-entry bar; dense single-topic section chunks clear it.
"""
import re

# Bump when chunking behavior changes - it is part of the ingest fingerprint
# (ingest_sync._ingest_fingerprint), so a bump forces every file to re-ingest on the
# next startup instead of being skipped as "unchanged".
CHUNKER_VERSION = "3"

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def chunk_plain(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks


_H2 = re.compile(r"^## ", re.MULTILINE)
_DATE_IN_HEADING = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})")


def chunk_dated_markdown(text: str) -> list[dict]:
    """Split on '## ' sections into [{'text', 'entry_date'}] chunks.
    entry_date is 'YYYY-MM-DD' when the section heading starts with a date
    (the session log's '## 2026-01-15 - ...' convention), else None.
    Falls back to plain chunking when the text has no '## ' sections."""
    starts = [m.start() for m in _H2.finditer(text)]
    if not starts:
        return [{"text": c, "entry_date": None} for c in chunk_plain(text)]
    out: list[dict] = []
    pre = text[:starts[0]].strip()
    if pre:
        out += [{"text": c, "entry_date": None} for c in chunk_plain(pre)]
    for s, e in zip(starts, starts[1:] + [len(text)]):
        section = text[s:e].strip()
        if not section:
            continue
        m = _DATE_IN_HEADING.match(section)
        entry_date = m.group(1) if m else None
        if len(section) <= CHUNK_SIZE + CHUNK_OVERLAP:
            out.append({"text": section, "entry_date": entry_date})
            continue
        heading, _, body = section.partition("\n")
        for piece in chunk_plain(body):
            out.append({"text": f"{heading}\n{piece}", "entry_date": entry_date})
    return out


def chunk_markdown_sections(text: str) -> list[str]:
    """Section-aware chunking for general markdown: one chunk per '## '
    section (heading kept with its body, oversized sections sub-chunked with
    the heading re-carried), preamble before the first '## ' on its own,
    plain-chunk fallback for text with no sections. Same algorithm as the
    history mode - the dated variant IS the section splitter, general
    ingestion just doesn't stamp dates (a general doc must not silently pick
    up recency decay from a dated heading)."""
    return [p["text"] for p in chunk_dated_markdown(text)]
