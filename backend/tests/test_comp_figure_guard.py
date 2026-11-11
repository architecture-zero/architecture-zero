"""The pay-figure guard, and federation in the audit row (2026-11-06).

1. THE COMP GUARD. The safety rules tell the model never to state, estimate or
   infer a pay figure; a model with the rule loaded on the turn can still
   work one out from numbers in front of it and state it, and one did. An
   instruction is a request, not a control. app/pii.py's _COMP_FIGURE is the control: a money amount next to
   a pay word is masked as [REDACTED:comp] in every answer, whatever the PII
   mode, and the audit row counts it (comp_out_masked).
2. PEER SOURCES. `sources` on the audit row is local retrieval only, so an
   answer built from a peer's documents read as "0 sources". The row now
   carries peer_sources - "peer: source" for every piece that reached the
   prompt; NULL when no peer was asked.

The FIX cases fail on the code before this change; the CONTROL cases pass on
both.
"""
import json
import random
import re
import uuid
from unittest.mock import patch

import pytest

from app.pii import OutputFilter

FIX = [
    "Contractor labor (net): ~$64K for the quarter.",
    "Your rate was $150/hr.",
    "The salary is $120,000 - $150,000 a year.",
    "He billed $95 an hour for the work.",
    "Base pay: 64,000 dollars, plus a bonus.",
    "Your consulting fee came to $14,500 in March.",
    "Total compensation was roughly $210K last year.",
]
CONTROL = [
    "A late fee of $35 applies.",
    "The repair labor was $300.",
    "Please pay $120 by Friday.",
    "The interest rate is 6.5%.",
    "Insurance costs $1,200 a year.",
    "The project took 6 months and 3 reviews.",
]
PAD_HEAD = "Some lead text here, then more of it. " * 4
PAD_TAIL = " And the answer goes on for a while after that point."


def _guard(text):
    return OutputFilter("off", comp=True).apply(text)


# -- 1. the guard ------------------------------------------------------------

def test_fix_a_derived_labor_figure_is_masked():
    out = _guard("Contractor labor (net): ~$64K for the quarter.")
    assert out == "Contractor labor (net): ~[REDACTED:comp] for the quarter."


@pytest.mark.parametrize("text", FIX)
def test_fix_a_pay_figure_is_masked(text):
    out = _guard(text)
    assert "[REDACTED:comp]" in out
    assert not any(ch.isdigit() for ch in out.replace("[REDACTED:comp]", ""))


@pytest.mark.parametrize("text", CONTROL)
def test_fix_ordinary_money_talk_passes_untouched(text):
    # FIX, not CONTROL: on the old code OutputFilter takes no comp argument.
    assert _guard(text) == text


@pytest.mark.parametrize("text", FIX + CONTROL)
def test_fix_streamed_output_equals_one_shot_at_every_split(text):
    padded = PAD_HEAD + text + PAD_TAIL
    ref = _guard(padded)
    for cut in range(1, len(padded)):
        f = OutputFilter("off", comp=True)
        assert f.push(padded[:cut]) + f.push(padded[cut:]) + f.flush() == ref, cut
    rnd = random.Random(7)
    for _ in range(100):
        f, out, i = OutputFilter("off", comp=True), [], 0
        while i < len(padded):
            n = rnd.randint(1, 9)
            out.append(f.push(padded[i:i + n]))
            i += n
        out.append(f.flush())
        assert "".join(out) == ref


def test_fix_a_figure_arriving_in_many_small_tokens_is_masked():
    f = OutputFilter("off", comp=True)
    pieces = ["Your consult", "ing fee", " came", " to ", "$1", "4,", "50", "0 overall", "."]
    out = "".join(f.push(p) for p in pieces) + f.flush()
    assert out == "Your consulting fee came to [REDACTED:comp] overall."
    assert f.comp_masked == 1


def test_fix_the_guard_runs_with_pii_off_and_counts():
    f = OutputFilter("off", comp=True)
    f.apply("Your rate was $150/hr and your retainer $2,000.")
    assert f.comp_masked == 2
    assert f.hits is None        # the PII half stays unknown with its mode off


def test_control_without_the_guard_the_filter_is_the_identity():
    f = OutputFilter("off")
    assert f.push("Your rate was $150/hr.") == "Your rate was $150/hr."
    assert f.flush() == ""


def test_fix_the_mode_is_on_unless_exactly_off():
    from app.pii import normalize_comp_mode
    assert normalize_comp_mode(None) == "redact"
    assert normalize_comp_mode("off") == "off"
    assert normalize_comp_mode(" OFF ") == "off"
    assert normalize_comp_mode("of") == "redact"      # a typo leaves the control on


def test_fix_the_receipt_sums_comp_and_is_null_when_the_guard_is_off():
    from app.runtime_config import _pii_receipt
    a = OutputFilter("off", comp=True); a.apply("Your rate was $150/hr.")
    b = OutputFilter("off", comp=True); b.apply("Your salary: $90K.")
    assert _pii_receipt(a, b)["comp_out_masked"] == 2
    off = OutputFilter("off"); off.apply("Your rate was $150/hr.")
    assert _pii_receipt(off)["comp_out_masked"] is None


# -- the chat router, end to end ------------------------------------------------

def _guest_enabled_cfg(key, default=None):
    return "true" if key == "guest_mode_enabled" else default


def _tokens(r):
    return [json.loads(l[6:])["token"] for l in r.text.splitlines()
            if l.startswith("data: ") and '"token"' in l]


def _without_error_id(body: str) -> str:
    """The response body with a failed turn's reference id taken out.

    That id is twelve random hex characters, and a check that a figure
    appears NOWHERE in the body reads it too: about one id in four hundred
    spells "150", which is how the check below once failed with nothing
    leaked. Only the id's own characters are removed - a figure anywhere
    else in the body is still found (the control after that test).
    """
    return re.sub(r'("error_id":\s*")[0-9a-f]{12}(")', r"\1\2", body)


def _pay_stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
    for piece in ("For the quarter: Contractor labor (n", "et): ~$6", "4K, give or take."):
        yield {"type": "text", "text": piece}


def _row(sid):
    from app.db import get_session
    from app.models import AuditLog
    with get_session() as db:
        row = db.query(AuditLog).filter_by(session_id=sid).one()
        return row.comp_out_masked, row.peer_sources, row.sources


def _post_guest(client, sid, monkeypatch, guard):
    import app.runtime_config as rc
    import app.routers.chat as chat_mod
    monkeypatch.setattr(rc, "PII_OUTPUT_MODE", "off")
    monkeypatch.setattr(rc, "COMP_FIGURE_GUARD", guard)
    monkeypatch.setattr(chat_mod, "ENABLE_AUDIT_LOG", True)
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_pay_stream):
        return client.post("/api/chat", json={"prompt": "What did I make?", "model": "test-model",
                                              "session_id": sid})


def test_fix_chat_masks_the_figure_with_pii_off_and_stamps_the_count(client, monkeypatch):
    sid = f"comp-{uuid.uuid4().hex[:8]}"
    r = _post_guest(client, sid, monkeypatch, "redact")
    assert r.status_code == 200
    text = "".join(_tokens(r))
    assert text == "For the quarter: Contractor labor (net): ~[REDACTED:comp], give or take."
    assert "64" not in r.text
    assert _row(sid)[0] == 1


def test_fix_chat_with_the_guard_off_passes_the_figure_and_stamps_null(client, monkeypatch):
    sid = f"comp-off-{uuid.uuid4().hex[:8]}"
    r = _post_guest(client, sid, monkeypatch, "off")
    assert "".join(_tokens(r)) == "For the quarter: Contractor labor (net): ~$64K, give or take."
    assert _row(sid)[0] is None


def _failing_stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "Half an answer: your rate was $150/hr, then"}
    raise ValueError("provider stream error")


def test_fix_a_failed_turn_still_shows_the_held_tail_masked(client, monkeypatch):
    # The guard holds the last characters back; a round that fails must
    # release them (through the filter) before the error, not drop them.
    import app.runtime_config as rc
    import app.routers.chat as chat_mod
    monkeypatch.setattr(rc, "PII_OUTPUT_MODE", "off")
    monkeypatch.setattr(rc, "COMP_FIGURE_GUARD", "redact")
    monkeypatch.setattr(chat_mod, "ENABLE_AUDIT_LOG", True)
    with patch("app.routers.chat.guest_chat_available", return_value=True), \
         patch("app.routers.chat.get_config", side_effect=_guest_enabled_cfg), \
         patch("app.routers.chat.stream_chat_events", side_effect=_failing_stream):
        r = client.post("/api/chat", json={"prompt": "Hi", "model": "test-model",
                                           "session_id": f"comp-err-{uuid.uuid4().hex[:8]}"})
    assert "".join(_tokens(r)) == "Half an answer: your rate was [REDACTED:comp]/hr, then"
    assert "150" not in _without_error_id(r.text)
    assert "failed to complete" in r.text


def test_the_figure_check_reads_the_whole_body_but_not_the_reference_id():
    # CONTROL for the check above. A failed turn's body ends in an error
    # event that carries a random reference id. An id that happens to spell
    # the figure must not fail the check; a figure anywhere else still must.
    event = "data: " + json.dumps({"error": "The response failed to complete.",
                                   "error_id": "ef27a1504fcc"}) + "\n\n"
    assert "150" in event                             # the id spells it
    assert "150" not in _without_error_id(event)      # ... and is not read
    leak = "data: " + json.dumps({"token": "your rate was $150/hr"}) + "\n\n" + event
    assert "150" in _without_error_id(leak)           # a real leak still is
    other = "data: " + json.dumps({"note": "150", "error_id": "0015015015ab"}) + "\n\n"
    assert _without_error_id(other).count("150") == 1   # only the id is removed


# -- 2. peer sources -----------------------------------------------------------

QUESTION = "What is the vendor onboarding turnaround?"


def _peer_pieces(n_peers=2, per_peer=3):
    return [{"peer": f"peer{p}", "source": f"doc-{p}-{i}.md", "score": 0.9,
             "text": f"peer piece {p}-{i}"} for p in range(n_peers) for i in range(per_peer)]


def _keep_all(query, pool, top_k=None, stats=None):
    return list(pool)


def _ok_stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "ok"}


def _chat_peers(client, admin_headers, monkeypatch, pieces, use_peers=True):
    import app.routers.chat as chat_mod
    monkeypatch.setattr(chat_mod, "ENABLE_AUDIT_LOG", True)
    sid = f"peer-src-{uuid.uuid4().hex[:8]}"
    peer_rows = [{"id": "p1", "name": "peer0", "url": "http://peer.test", "enabled": True}]
    with patch("app.routers.chat.get_peers", return_value=peer_rows), \
         patch("app.routers.chat.query_peer_kb", return_value=list(pieces)), \
         patch("app.rerank.retrieve", return_value=[]), \
         patch("app.rerank.rerank", side_effect=_keep_all), \
         patch("app.routers.chat.stream_chat_events", side_effect=_ok_stream):
        r = client.post("/api/chat", headers=admin_headers, json={
            "prompt": QUESTION, "use_peers": use_peers, "use_rag": True, "session_id": sid})
    assert r.status_code == 200
    return sid


def test_fix_an_answer_built_from_peers_records_them(client, admin_headers, monkeypatch):
    sid = _chat_peers(client, admin_headers, monkeypatch, _peer_pieces())
    _, peer_sources, sources = _row(sid)
    assert json.loads(sources) == []          # local retrieval found nothing
    assert json.loads(peer_sources) == [f"peer{p}: doc-{p}-{i}.md" for p in range(2) for i in range(3)]


def test_fix_peers_asked_and_none_kept_records_an_empty_list(client, admin_headers, monkeypatch):
    below = [{**p, "score": 0.01} for p in _peer_pieces()]
    sid = _chat_peers(client, admin_headers, monkeypatch, below)
    assert json.loads(_row(sid)[1]) == []


def test_fix_no_peer_asked_records_null(client, admin_headers, monkeypatch):
    sid = _chat_peers(client, admin_headers, monkeypatch, _peer_pieces(), use_peers=False)
    assert _row(sid)[1] is None


def test_fix_the_read_view_and_the_export_carry_both_columns(client):
    from app.audit import export_audit_csv, get_audit_log, log_audit_entry
    sid = f"cols-{uuid.uuid4().hex[:8]}"
    log_audit_entry(user_id=1, username="colsrow", session_id=sid, prompt="q",
                    response_length=3, model="m", use_rag=True, sources=[],
                    answer_lane="model", comp_out_masked=1, peer_sources=["peer0: a.md"])
    listed = next(e for e in get_audit_log(page=1, page_size=10, username_filter="colsrow")["entries"]
                  if e["session_id"] == sid)
    assert listed["comp_out_masked"] == 1 and listed["peer_sources"] == ["peer0: a.md"]
    lines = list(export_audit_csv(username_filter="colsrow"))
    assert lines[0].rstrip().endswith("comp_out_masked,peer_sources")
    assert any(sid in l and l.rstrip().endswith('1,"[""peer0: a.md""]"') for l in lines[1:])


def test_fix_status_names_the_guard(client, admin_headers):
    body = client.get("/api/status", headers=admin_headers).json()
    assert body["comp_figure_guard"] == "redact"     # on by default


def test_fix_the_posture_record_names_the_guard(client):
    import app.system_records as sr
    posture = sr.build_posture(sr._snapshot())
    assert "- Pay-figure guard on answers: redact" in posture
