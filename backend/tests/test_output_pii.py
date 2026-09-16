"""Output-side PII (2026-09-16).

Before this, the one control on text LEAVING in an answer was the content
blocklist, applied to each streamed token on its own - and a regex applied per
token can never match what a token boundary splits. Pins, in the order the
data flows:

  1. the stream filter's output equals one-shot redaction of the full text at
     EVERY split point (the property everything else rests on);
  2. a match is acted on only once its trailing boundary is known;
  3. the blocklist rides the same buffer, so a split term is caught;
  4. off is the identity, warn counts without touching the text, redact masks
     only the listed types and counts the rest;
  5. config normalises: unknown mode -> off, unknown types ignored;
  6. the receipt columns round-trip through the audit writer and default NULL;
  7. the chat router streams the filter's output and its flush tail, stamps
     the receipt on the audit row, and /api/status names the mode;
  8. no per-token blocklist call is left in the router or the eval engine,
     and every streamed round flushes its filter.
"""
import inspect
import json
import random
import uuid
from unittest.mock import patch

import pytest

from app import pii
from app.pii import (OutputFilter, redact_output, redact_pii,
                     normalize_output_mode, parse_redact_types)

ALL = ["SSN", "credit_card", "email", "phone", "IP_address"]

TEXTS = [
    "My SSN is 123-45-6789, and my card 4111 1111 1111 1111 expires soon.",
    "Call 555-12345 or (555) 555-1234 or mail hr@acme.example today",
    "Server 10.0.0.1 and version 1.2.3.4 and 10.0.0.1.5 tail",
    "no pii here at all, just words and numbers 12345 67890",
    "ends with an ssn 123-45-6789",
    "ends with email someone.very.long.local.part+tag@example.co.uk",
    "abc123-45-6789 glued prefix and 123-45-67890 too long, 4111-1111-1111-1111.",
    "Acme's HR line is 555-123-4567; the Acme portal is 192.168.0.9.",
    "a" * 60 + " 123-45-6789 " + "b" * 40,
]


def _stream(text, n=None, seed=None, **kw):
    f = OutputFilter(**kw)
    out = []
    if seed is not None:
        rng = random.Random(seed)
        i = 0
        while i < len(text):
            step = rng.randint(1, 9)
            out.append(f.push(text[i:i + step]))
            i += step
    else:
        for i in range(0, len(text), n):
            out.append(f.push(text[i:i + n]))
    out.append(f.flush())
    return "".join(out), f


# ── 1. streamed == one-shot at every split point ─────────────────────────────

@pytest.mark.parametrize("text", TEXTS)
def test_streamed_output_equals_one_shot_at_every_split(text):
    kw = dict(mode="redact", redact_types=ALL, blocklist=["acme"])
    ref = redact_output(text, **kw)
    for n in range(1, 8):
        out, _ = _stream(text, n=n, **kw)
        assert out == ref, (n, out, ref)
    for seed in range(20):
        out, _ = _stream(text, seed=seed, **kw)
        assert out == ref, (seed, out, ref)


def test_one_shot_matches_the_ingest_redactor_on_plain_matches():
    text = "ssn 123-45-6789 mail a@b.io ip 10.0.0.1 card 4111 1111 1111 1111 tel 555-123-4567."
    ours = redact_output(text, "redact", ALL)
    for kind in ALL:
        ours = ours.replace(f"[REDACTED:{kind}]", "[REDACTED]")
    assert ours == redact_pii(text)


# ── 2. settled only ──────────────────────────────────────────────────────────

def test_trailing_boundary_is_judged_against_the_real_next_character():
    f = OutputFilter("redact", ALL)
    out = f.push("ext 555-1234")
    assert out == ""
    out += f.push("5 is not a phone number") + f.flush()
    assert out == "ext 555-12345 is not a phone number"
    assert f.hits == {} and f.redacted == 0


def test_leading_boundary_survives_the_cut():
    text = "x" * 30 + "abc123-45-6789 and " + "y" * 30
    for n in (1, 3, 7):
        out, f = _stream(text, n=n, mode="redact", redact_types=ALL)
        assert out == text and f.redacted == 0


# ── 3. the blocklist rides the same buffer ───────────────────────────────────

def test_blocklist_term_split_across_tokens_is_caught():
    f = OutputFilter("off", blocklist=["acme"])
    out = f.push("Ask Ac") + f.push("me about it, and AC") + f.push("ME again.")
    out += f.flush()
    assert out == "Ask [BLOCKED] about it, and [BLOCKED] again."
    assert f.hits is None


# ── 4. the three modes ───────────────────────────────────────────────────────

def test_off_is_the_identity_with_no_hold():
    f = OutputFilter("off")
    assert f.push("123-45-6789 and 4111 1111 1111 1111") == "123-45-6789 and 4111 1111 1111 1111"
    assert f.flush() == ""
    assert f.hits is None and f.types is None and f.redacted == 0


def test_warn_counts_without_touching_or_delaying_the_text():
    f = OutputFilter("warn")
    assert f.push("ssn 123-45-6789 mail a@b.io") == "ssn 123-45-6789 mail a@b.io"
    assert f.flush() == ""
    assert f.hits == {"SSN": 1, "email": 1} and f.redacted == 0


def test_redact_masks_the_listed_types_and_counts_the_rest():
    out, f = _stream("ssn 123-45-6789 card 4111-1111-1111-1111 mail a@b.io ip 10.0.0.1 end",
                     n=4, mode="redact")
    assert out == "ssn [REDACTED:SSN] card [REDACTED:credit_card] mail a@b.io ip 10.0.0.1 end"
    assert f.hits == {"SSN": 1, "credit_card": 1, "email": 1, "IP_address": 1}
    assert f.redacted == 2


# ── 5. config normalisation ──────────────────────────────────────────────────

def test_mode_and_types_normalise():
    assert normalize_output_mode(None) == "off"
    assert normalize_output_mode(" Redact ") == "redact"
    assert normalize_output_mode("false") == "off"
    assert parse_redact_types(None) == (["SSN", "credit_card"], [])
    assert parse_redact_types("ssn, Email,dob") == (["SSN", "email"], ["dob"])
    longest = max(len(s) for s in ("+1 (555) 555-1234", "4111 1111 1111 1111",
                                    "255.255.255.255", "123-45-6789"))
    assert pii._HOLD > longest


# ── 6. the receipt columns ───────────────────────────────────────────────────

def test_receipt_columns_round_trip_through_the_writer_and_default_null(client):
    from app.audit import log_audit_entry, get_audit_log
    from app.db import get_session
    from app.models import AuditLog
    sid = f"pii-{uuid.uuid4().hex[:8]}"
    log_audit_entry(user_id=1, username="piirow", session_id=sid, prompt="q",
                    response_length=3, model="m", use_rag=False, sources=[],
                    answer_lane="model", pii_out_hits=3, pii_out_redacted=1,
                    pii_out_types="SSN,email")
    log_audit_entry(user_id=1, username="piirow", session_id=sid + "-off", prompt="q",
                    response_length=3, model="m", use_rag=False, sources=[],
                    answer_lane="model")
    with get_session() as db:
        row = db.query(AuditLog).filter_by(session_id=sid).one()
        assert (row.pii_out_hits, row.pii_out_redacted, row.pii_out_types) == (3, 1, "SSN,email")
        off = db.query(AuditLog).filter_by(session_id=sid + "-off").one()
        assert (off.pii_out_hits, off.pii_out_redacted, off.pii_out_types) == (None, None, None)
    page = get_audit_log(page=1, page_size=10, username_filter="piirow")
    listed = next(e for e in page["entries"] if e["session_id"] == sid)
    assert listed["pii_out_hits"] == 3 and listed["pii_out_types"] == "SSN,email"


def test_receipt_helper_sums_rounds_and_is_null_shaped_when_off():
    from app.runtime_config import _pii_receipt
    a = OutputFilter("redact", ALL); a.apply("ssn 123-45-6789.")
    b = OutputFilter("redact", ALL); b.apply("mail a@b.io and 123-45-6789.")
    assert _pii_receipt(a, b) == {"pii_out_hits": 3, "pii_out_redacted": 3,
                                  "pii_out_types": "SSN,email"}
    off = OutputFilter("off"); off.apply("123-45-6789")
    assert _pii_receipt(off) == {"pii_out_hits": None, "pii_out_redacted": None,
                                 "pii_out_types": None}
    assert _pii_receipt() == _pii_receipt(off)


# ── 7. the chat router, end to end ───────────────────────────────────────────

def _split_stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
    # An SSN split across three events, then an email the default set only counts.
    for piece in ("Your SSN is 123-", "45-67", "89 and HR is hr@", "corp.example.", " Done."):
        yield {"type": "text", "text": piece}


def _guest_enabled_cfg(key, default=None):
    return "true" if key == "guest_mode_enabled" else default


def _tokens(r):
    return [json.loads(l[6:])["token"] for l in r.text.splitlines()
            if l.startswith("data: ") and '"token"' in l]


def _post(client, sid):
    return client.post("/api/chat", json={"prompt": "Read it back", "model": "test-model",
                                          "session_id": sid})


def test_chat_streams_redacted_text_and_stamps_the_receipt(client, monkeypatch):
    import app.runtime_config as rc
    import app.routers.chat as chat_mod
    from app.db import get_session
    from app.models import AuditLog
    monkeypatch.setattr(rc, "PII_OUTPUT_MODE", "redact")
    monkeypatch.setattr(chat_mod, "ENABLE_AUDIT_LOG", True)
    sid = f"pii-chat-{uuid.uuid4().hex[:8]}"
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_split_stream):
        r = _post(client, sid)
    assert r.status_code == 200
    text = "".join(_tokens(r))
    assert text == "Your SSN is [REDACTED:SSN] and HR is hr@corp.example. Done."
    assert "6789" not in r.text
    assert "[DONE]" in r.text
    with get_session() as db:
        row = db.query(AuditLog).filter_by(session_id=sid).one()
        assert (row.pii_out_hits, row.pii_out_redacted, row.pii_out_types) == (2, 1, "SSN,email")
        assert row.response_length == len(text)


def test_chat_off_mode_streams_events_unchanged_with_a_null_receipt(client, monkeypatch):
    import app.runtime_config as rc
    import app.routers.chat as chat_mod
    from app.db import get_session
    from app.models import AuditLog
    monkeypatch.setattr(rc, "PII_OUTPUT_MODE", "off")
    monkeypatch.setattr(chat_mod, "ENABLE_AUDIT_LOG", True)
    sid = f"pii-off-{uuid.uuid4().hex[:8]}"
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_split_stream):
        r = _post(client, sid)
    assert _tokens(r) == ["Your SSN is 123-", "45-67", "89 and HR is hr@", "corp.example.", " Done."]
    with get_session() as db:
        row = db.query(AuditLog).filter_by(session_id=sid).one()
        assert (row.pii_out_hits, row.pii_out_redacted, row.pii_out_types) == (None, None, None)


def test_status_names_the_output_mode_and_the_masked_types(client, admin_headers, monkeypatch):
    import app.routers.system as sys_mod
    monkeypatch.setattr(sys_mod, "PII_OUTPUT_MODE", "redact")
    body = client.get("/api/status", headers=admin_headers).json()
    assert body["pii_output_mode"] == "redact"
    assert body["pii_output_redact_types"] == list(sys_mod.PII_OUTPUT_REDACT_TYPES)


# ── 8. the wiring ────────────────────────────────────────────────────────────

def test_no_per_token_blocklist_call_is_left_and_every_round_flushes():
    import app.routers.chat as chat_mod
    import app.eval_runner as ev
    assert "apply_blocklist" not in inspect.getsource(chat_mod)
    assert "apply_blocklist" not in inspect.getsource(ev)
    chat_src = inspect.getsource(chat_mod.chat)
    # Two streamed rounds (the tool loop and the empty-answer retry), one
    # filter and one flush each; one audit site stamps the receipt (the model
    # lane) - the refusal lane serves canned text and records NULL by omission.
    assert chat_src.count("_output_filter()") == 2
    assert chat_src.count(".flush()") == 2
    assert chat_src.count("**_pii_receipt(") == 1
