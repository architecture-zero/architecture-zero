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


def apply_blocklist(text: str, blocklist: list[str]) -> str:
    """Replace blocked terms with [BLOCKED] (case-insensitive)."""
    if not blocklist:
        return text
    for term in blocklist:
        text = re.sub(re.escape(term), "[BLOCKED]", text, flags=re.IGNORECASE)
    return text
