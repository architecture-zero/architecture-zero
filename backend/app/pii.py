"""PII detection, redaction, and content safety utilities.

Patterns cover the most common regulated data types.
False positives are possible - warn/redact modes are designed for human review,
not as a substitute for legal compliance review.
"""
import re

_PATTERNS: dict[str, re.Pattern] = {
    "SSN":         re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    "credit_card": re.compile(r'\b(?:\d{4}[\s\-]?){3}\d{4}\b'),
    "email":       re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),
    "phone":       re.compile(r'\b(\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]\d{4}\b'),
    "IP_address":  re.compile(r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'),
}


def scan_pii(text: str) -> list[dict]:
    """Return [{type, count}] for each PII pattern found in text."""
    findings = []
    for pii_type, pattern in _PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings.append({"type": pii_type, "count": len(matches)})
    return findings


def redact_pii(text: str) -> str:
    """Replace all detected PII with [REDACTED]."""
    for pattern in _PATTERNS.values():
        text = pattern.sub("[REDACTED]", text)
    return text


def build_blocklist(raw: str) -> list[str]:
    """Parse CONTENT_SAFETY_BLOCKLIST env var into a list of lowercased terms."""
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


# ── Output side (2026-09-16, the product tail's third item) ─────────────────
#
# The scanner above runs on text ENTERING the corpus. Nothing ran on text
# LEAVING in an answer: the one output control was apply_blocklist (deleted
# 2026-10-03, uncalled since this filter replaced it), applied
# to each streamed token on its own - and a regex applied per token can never
# match anything a token boundary splits ("123-45-" + "6789" is two
# non-matches). The same hole let a blocklisted term through whenever the
# model tokenised it in two pieces.
#
# OutputFilter closes both by buffering the stream: every token goes in, text
# comes out only once the filter knows nothing still arriving can change what
# it emits. Three rules make streamed output identical to one-shot redaction
# of the full answer (the property test_output_pii pins at every split point):
#
#   1. HOLD - the last _HOLD characters stay in the buffer, so a match that is
#      still being spelled out (a card number three groups in) is never
#      partly emitted. 24 covers the longest pattern with room to spare.
#   2. NO SPLIT INSIDE A RUN - the cut never lands inside an unbroken run of
#      word-ish characters (letters, digits, and the email alphabet), so a
#      leading \b is always judged against the character that really precedes
#      the run, and an email's local part is held whole however long it is.
#   3. SETTLED ONLY - a match is acted on only when at least one character
#      follows it in the buffer (or the stream has ended), so the trailing \b
#      is judged against the real next character: "555-1234" followed by "5"
#      is not a phone number, and the filter waits to find out.
#
# Modes mirror PII_SCAN_MODE's vocabulary and are configured separately
# (PII_OUTPUT_MODE), because the two surfaces are ruled per instance on
# different grounds - a private owner-only instance scans nothing at ingest
# because the corpus IS the owner's data, and redacts nothing on output because
# the owner is the only reader; a public-facing instance redacts. 'warn'
# counts, 'redact' masks the types in redact_types and counts
# the rest; both leave a receipt (hits / redacted / types) for the audit row.
# Off with an empty blocklist is the identity: no buffering, no delay.
#
# This is regex-class detection (the five patterns above, false positives
# included), not entity recognition - the same honesty note the ingest side
# carries. It stops the obvious echo (a number the model repeats from a
# document, a tool result, or the user's own prompt); it does not stop a
# model that spells a number out in words.

# ── Compensation figures (2026-11-06) ───────────────────────────────────────
#
# The safety rules tell the model never to state, estimate or infer a pay
# figure - and an instruction is a request, not a control: a model with the
# rule loaded on the turn can still work a figure out from numbers in front of
# it and state it, and one did. This is the
# control behind that rule: a money amount next to a PAY word is masked as
# [REDACTED:comp] before the reader sees it, whatever the model decided.
#
# The word list is deliberately narrow, because ordinary money talk is not pay:
# "a late fee of $35", "the repair labor was $300", "pay $120 by Friday", "an
# interest rate of 6.5%" all pass. Bare "fee", "rate", "labor", "pay",
# "income" and "earnings" count only with an owner word in front ("your fee",
# "my rate", "contractor labor", "annual income"); salary, wages,
# compensation, comp, paycheck, take-home, base pay and retainer count alone;
# and an amount followed by an hourly marker ("$150/hr", "$95 an hour") counts
# with no word at all. Regex-class like the PII patterns above: it masks a
# figure in digits, not one spelled out in words.
_AMOUNT = r"\d[\d,]*(?:\.\d+)?(?:\s?[kKmM]\b)?"
_MONEY = (rf"(?:\$\s?{_AMOUNT}(?:\s?(?:-|to|and)\s?\$?\s?{_AMOUNT})?"   # $64K, $120,000 - $150,000
          r"|\b\d[\d,]*(?:\.\d+)?\s?(?:[kK]\s?)?(?:dollars|USD)\b)")     # 64,000 dollars
_OWNER = r"(?:my|his|her|your|their|our|the owner's|annual|yearly|hourly|daily|day|monthly)"
_PAY_WORD = (r"(?:salary|salaries|wages?|compensation|comp|paycheck|take-home(?:\s+pay)?|base\s+pay|retainer"
             rf"|{_OWNER}\s+(?:pay|rate|fees?|earnings|income|labor)"
             r"|(?:bill(?:ing)?|pay|contract(?:or)?|consulting)\s+rate"
             r"|(?:contract(?:or)?|consulting|project)\s+(?:fees?|labor))")
_COMP_FIGURE = re.compile(
    rf"\b{_PAY_WORD}\b[^\n.$]{{0,40}}?(?P<lead>{_MONEY})"
    rf"|(?P<trail>{_MONEY})\s?(?:/\s?(?:hr|hour)\b|(?:an|per)\s+hour\b|hourly\b)",
    re.IGNORECASE)
COMP_MODES = ("off", "redact")
_COMP_KIND = "comp"
# A comp match can reach ~70 characters past its amount's first digit (a range
# plus an hourly marker) and starts up to ~60 before it (the pay word and the
# gap), so a filter with the comp guard on holds more and keeps the text it
# already emitted as left context for the pay word.
_COMP_HOLD = 80
_COMP_CTX = 80


def normalize_comp_mode(raw: str | None) -> str:
    """'redact' unless set to exactly 'off'. The PII mode falls back to off on
    a typo; this one falls back to ON - it backs a safety rule, so a mistyped
    value must leave the control working, never silently disable it."""
    mode = (raw or "redact").strip().lower()
    return mode if mode in COMP_MODES else "redact"


def comp_figure_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every pay figure's AMOUNT in text - the part masked."""
    spans = []
    for m in _COMP_FIGURE.finditer(text):
        g = "lead" if m.group("lead") is not None else "trail"
        spans.append((m.start(g), m.end(g)))
    return spans


_HOLD = 24
_MARK_OUT = "[REDACTED:{}]"
_BLOCK_MARK = "[BLOCKED]"
_RUN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                       "0123456789._%+-@")
OUTPUT_MODES = ("off", "warn", "redact")
DEFAULT_REDACT_TYPES = ("SSN", "credit_card")


def normalize_output_mode(raw: str | None) -> str:
    """The configured mode, or 'off' for anything outside OUTPUT_MODES. The
    caller logs the fall-back; the status surfaces show the normalized value,
    so a typo reads OFF in the open rather than half-working in the dark."""
    mode = (raw or "off").strip().lower()
    return mode if mode in OUTPUT_MODES else "off"


def parse_redact_types(raw: str | None) -> tuple[list[str], list[str]]:
    """(known types, unknown names) from a comma-separated PII_OUTPUT_REDACT_TYPES.
    Unset or blank means the default pair; an explicit list is taken as given,
    so an operator who wants email masked adds it, and one who wants nothing
    masked but everything counted sets 'warn' instead."""
    if raw is None or not raw.strip():
        return list(DEFAULT_REDACT_TYPES), []
    known, unknown = [], []
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        match = next((k for k in _PATTERNS if k.lower() == name.lower()), None)
        (known.append(match) if match else unknown.append(name))
    return known, unknown


class OutputFilter:
    """Stream-aware redaction for one answer. push() per token, flush() once
    at the end (it returns the held tail - dropping it truncates the answer).

    After flush(): .hits maps every detected type to its count over the
    ORIGINAL text, .redacted is how many spans were masked, .types is the
    sorted list of detected types. All three are None-shaped (hits == None)
    when the mode is off, so the audit row records unknown, never 0.
    """

    def __init__(self, mode: str = "off",
                 redact_types: list[str] | tuple[str, ...] | None = None,
                 blocklist: list[str] | None = None,
                 comp: bool = False):
        self.mode = normalize_output_mode(mode)
        self.redact_types = frozenset(redact_types if redact_types is not None
                                      else DEFAULT_REDACT_TYPES)
        self.blocklist = [t for t in (blocklist or []) if t]
        self._scan = self.mode != "off"
        self._mask = self.mode == "redact"
        # The comp guard (see _COMP_FIGURE) is independent of the PII mode: a
        # private instance runs PII off and still never states a pay figure.
        self.comp = bool(comp)
        self._hold = _COMP_HOLD if self.comp else _HOLD
        self._ctx = ""            # the tail of what was already emitted (original text)
        self.comp_masked: int | None = 0 if self.comp else None
        # Buffering is needed to MASK (PII, blocklist or comp); counting alone
        # reads the finished text once at flush, so 'warn' costs no delay.
        self._buffer = self._mask or bool(self.blocklist) or self.comp
        self._buf = ""
        self._orig: list[str] = []
        self.redacted = 0
        self.hits: dict[str, int] | None = None
        self.types: list[str] | None = None
        self._done = False

    # -- the two calls a lane makes ---------------------------------------
    def push(self, text: str) -> str:
        if not text:
            return ""
        if self._scan:
            self._orig.append(text)
        if not self._buffer:
            return text
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> str:
        if self._done:
            return ""
        self._done = True
        out = self._drain(final=True) if self._buffer else ""
        if self._scan:
            findings = scan_pii("".join(self._orig))
            self.hits = {f["type"]: f["count"] for f in findings}
            self.types = sorted(self.hits)
        return out

    # -- one-shot convenience for the non-streaming lanes -----------------
    def apply(self, text: str) -> str:
        return self.push(text) + self.flush()

    # -- internals ---------------------------------------------------------
    def _spans(self, buf: str) -> list[tuple[int, int, str]]:
        spans = []
        if self._mask:
            for kind, pattern in _PATTERNS.items():
                if kind in self.redact_types:
                    for m in pattern.finditer(buf):
                        spans.append((m.start(), m.end(), kind))
        for term in self.blocklist:
            for m in re.finditer(re.escape(term), buf, flags=re.IGNORECASE):
                spans.append((m.start(), m.end(), _BLOCK_MARK))
        if self.comp:
            # Matched over the emitted tail + the buffer, so a pay word that
            # already went out still marks the amount that follows it; only
            # the amount's part inside the buffer can be masked.
            off = len(self._ctx)
            for start, end in comp_figure_spans(self._ctx + buf):
                if end - off > 0:
                    spans.append((max(start - off, 0), end - off, _COMP_KIND))
        # Longest span first at a shared start, so an overlap resolves to the
        # bigger match - the one-shot order redact_pii would also reach.
        spans.sort(key=lambda s: (s[0], -s[1]))
        return spans

    def _drain(self, final: bool) -> str:
        buf = self._buf
        if final:
            cut = len(buf)
        else:
            cut = len(buf) - self._hold
            while cut > 0 and buf[cut - 1] in _RUN_CHARS:   # rule 2
                cut -= 1
            if cut <= 0:
                return ""
        out: list[str] = []
        pos = 0
        for start, end, kind in self._spans(buf):
            if start >= cut:
                break                      # entirely in the held tail: next time
            if start < pos:
                continue                   # swallowed by a longer earlier span
            if not final and end >= len(buf):                # rule 3
                cut = start                # unsettled: hold from here on
                break
            out.append(buf[pos:start])
            if kind == _BLOCK_MARK:
                out.append(_BLOCK_MARK)
            elif kind == _COMP_KIND:
                out.append(_MARK_OUT.format(_COMP_KIND))
                self.comp_masked += 1
            else:
                out.append(_MARK_OUT.format(kind))
                self.redacted += 1
            pos = end
            if end > cut:
                cut = end                  # a settled span may cross the cut
        if pos < cut:
            out.append(buf[pos:cut])
        if self.comp:
            self._ctx = (self._ctx + buf[:cut])[-_COMP_CTX:]
        self._buf = buf[cut:]
        return "".join(out)


def redact_output(text: str, mode: str = "redact",
                  redact_types: list[str] | tuple[str, ...] | None = None,
                  blocklist: list[str] | None = None,
                  comp: bool = False) -> str:
    """One-shot form of OutputFilter for callers that hold the whole answer."""
    return OutputFilter(mode, redact_types, blocklist, comp).apply(text)
