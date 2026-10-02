"""The peer boundary: what a peer sent, checked before any of it reaches an answer.

Peer pieces arrive over HTTP at chat time and never pass the add_document choke
point, so the ingest-side injection gate never sees them. Until 2026-09-30 the
boundary scanned a piece's text but read text of any size (one of the scan's
rules is quadratic on a run of line breaks), never read the piece's NAME (which
is written into the prompt beside the text), tagged the peer's own dict in
place, and let a malformed piece - a list where an object belongs, a score that
is not a number - raise inside the answer.

The policy is unchanged: a piece with a HIGH finding is dropped from THIS answer
and logged loudly (the peer's corpus is not ours to quarantine); a milder
finding rides along tagged, and format_peer_context says so in its label.

Since 2026-10-02 the lane's last step is the RANK (`select`): the instance's
own cross-encoder keeps its top_k across every peer's pieces together, where
before every piece over the threshold reached the prompt.
"""
import os
import re
import unicodedata

# PIECE_MAX_CHARS: the longest piece text kept. A peer's pieces are chunks of
# its corpus, a few hundred to a few thousand characters; a piece past this is
# not a piece. 0 switches the bound off.
PIECE_MAX_CHARS = int(os.getenv("PEER_PIECE_MAX_CHARS", "4000"))
# SOURCE_NAME_MAX: a piece's name reaches the prompt in its label, the reader
# in the citation and the log in every event, and nothing bounded it.
SOURCE_NAME_MAX = 512
# POOL_MAX: how many pieces one answer's pool may hold, whatever the peers sent
# (with the rank, 2026-10-02): the rank reads every piece in the pool, and
# nothing else bounded how many.
POOL_MAX = 64

_NOT_IN_A_NAME = ("Cc", "Zl", "Zp")


def piece_text(chunk) -> str:
    """A piece's text, or nothing. A peer's JSON is not validated on the way
    in, so `text` can arrive as a list or a number; that is not a piece, and
    it must not be able to raise inside an answer."""
    text = chunk.get("text") if isinstance(chunk, dict) else None
    return text if isinstance(text, str) else ""


def score_of(chunk) -> float:
    """A piece's similarity score, or zero. The field is the peer's, so it can
    be anything; a score that is not a number clears no threshold. An integer
    of a few hundred digits is a number Python will not make a float of - it
    raises OverflowError, which would otherwise reach the answer as a 500."""
    try:
        return float(chunk.get("score", 0.0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0


def why_not_a_piece(chunk) -> str | None:
    """None when this boundary will read `chunk` as a piece; otherwise the
    reason it will not, in words a log line can carry. A piece is a dict whose
    `text` is a non-empty string no longer than PIECE_MAX_CHARS, and whose
    `source`, where it names one, is a string of at most SOURCE_NAME_MAX
    characters on one line."""
    if not isinstance(chunk, dict):
        return "not an object"
    text = chunk.get("text")
    if not isinstance(text, str) or not text.strip():
        return "no text"
    if PIECE_MAX_CHARS > 0 and len(text) > PIECE_MAX_CHARS:
        return "text over the size bound"
    source = chunk.get("source", "")
    if not isinstance(source, str):
        return "a source that is not a name"
    if len(source) > SOURCE_NAME_MAX:
        return "a source name over the size bound"
    if any(unicodedata.category(ch) in _NOT_IN_A_NAME for ch in source):
        return "a line break or control character in the source name"
    return None


def is_piece(chunk) -> bool:
    return why_not_a_piece(chunk) is None


def scan_mode() -> str:
    """The injection scan's mode AS THIS BOUNDARY READS IT: off, tag, or
    quarantine. Anything else - a typo in the host env - is quarantine: at the
    ingest gate the content is the operator's to review later, but here it is
    a peer's text on its way into an answer, and the safe reading of a setting
    nobody can parse is the strict one."""
    from app import corpus_scan
    mode = str(getattr(corpus_scan, "INJECTION_SCAN_MODE", "") or "").strip().lower()
    return mode if mode in ("off", "tag") else "quarantine"


def _short(value, n: int = 200) -> str:
    """A peer's string, cut for a log line."""
    return str(value)[:n]


_WHITESPACE_RUN = re.compile(r"\s{3,}")


def _for_scan(text: str) -> str:
    """The copy of a peer's text the scan reads: every run of three or more
    whitespace characters cut to its first two. ONE of the scan's rules is
    quadratic on a run of line breaks (measured 2026-09-28: a letter followed
    by line breaks cost 3.0 s at 20,000 characters; cut first, the worst of
    fifteen shapes is 0.009 s). It cannot hide a finding: no rule
    needs three whitespace characters in a row, and the rules that measure a
    distance can only match more. The model is handed the text as the peer
    wrote it; this is the scan's copy."""
    return _WHITESPACE_RUN.sub(lambda m: m.group(0)[:2], text)


def _findings(chunk: dict) -> list[dict]:
    """What the scan finds in a piece: in its text AND in its name. The name is
    the peer's too, and it is written into the prompt beside the text."""
    from app import corpus_scan
    found = list(corpus_scan.scan(_for_scan(piece_text(chunk))))
    name = chunk.get("source", "")
    if isinstance(name, str) and name:
        seen = {(f["type"], f["severity"]) for f in found}
        found += [f for f in corpus_scan.scan(_for_scan(name))
                  if (f["type"], f["severity"]) not in seen]
    return found


def admit(pieces, threshold: float) -> list[dict]:
    """What a peer sent, reduced to the pieces that clear `threshold`: anything
    that is not a piece goes first (before any field of it is read), then the
    score filter, which reads a score that cannot raise."""
    return [c for c in (pieces or []) if is_piece(c) and score_of(c) >= threshold]


def select(query: str, pieces, threshold: float, rank, on_event=None,
           report: dict | None = None) -> list[dict]:
    """The peer lane as the chat route runs it: admit (what is not a piece,
    then the score filter), the scan, then `rank(query, pool)` - the
    instance's own cross-encoder rerank, which keeps its top_k across every
    peer's pieces together. ONE function (2026-10-02), so a probe that reads
    the lane calls the lane and not a copy of it.

    WHY THE RANK: each peer sends its nearest eight pieces and the similarity
    threshold keeps nearly all of them, so three peers put twenty-four pieces
    in front of the model whatever the question. Read on a live three-peer
    deployment 2026-10-02 (six questions, no model call): 24 sent and 24 kept
    on every question, about 21,000 characters, the threshold removing none.
    On the first live run of a federated hub (2026-09-16) the model hedged
    "the context does not contain..." before quoting the very document that
    answered, and the rank is what that hub took. rerank() falls back to the
    input order, truncated to top_k, when the encoder is off or fails, so the
    bound holds either way.

    `report`, when given, is filled with the counts at each step - the route
    logs them and a probe reads them; nothing else does.

    THE POOL IS BOUNDED (POOL_MAX), before the scan and the
    rank read it: query_peer_kb keeps what it asked for from each peer, and this
    is the lane's own bound for a caller that hands it more - every piece kept
    is scanned, and the rank is a cross-encoder pass over all of them."""
    sent = pieces if isinstance(pieces, list) else []
    admitted = admit(sent, threshold)
    capped = max(0, len(admitted) - POOL_MAX)
    if capped:
        admitted = admitted[:POOL_MAX]
    pool = scan_pieces(admitted, on_event) if admitted else []
    kept = rank(query, pool) if pool else []
    if report is not None:
        report.update({"sent": len(sent), "admitted": len(admitted), "capped": capped,
                       "scanned": len(pool), "kept": len(kept)})
    return kept


def scan_pieces(pieces, on_event=None) -> list[dict]:
    """The injection gate at the peer boundary. A piece with a HIGH finding is
    dropped from this answer (quarantine mode) and reported; a milder finding
    rides along tagged. A piece is COPIED before it is tagged - the caller's
    list is its own. Something that is not a piece is dropped whatever the
    mode."""
    from app import corpus_scan
    mode = scan_mode()
    out: list[dict] = []
    for c in pieces or []:
        if not is_piece(c):
            continue
        if mode == "off":
            out.append(c)
            continue
        findings = _findings(c)
        if findings and corpus_scan.has_high(findings) and mode == "quarantine":
            if on_event:
                on_event("peer_chunk_blocked", peer=_short(c.get("peer", "?")),
                         source=_short(c.get("source", "?")),
                         types=corpus_scan.finding_types(findings))
            continue
        if findings:
            c = {**c, "injection_flagged": True}
            if on_event:
                on_event("injection_detected", source=_short(c.get("source", "?")),
                         trust="external", peer=_short(c.get("peer", "?")),
                         types=corpus_scan.finding_types(findings),
                         quarantined=False, mode=mode)
        out.append(c)
    return out
