"""The honesty judge's HYBRID persona input - the faithfulness rubric's
hybrid rule applied to the honesty rubric. A persona instance's refusal can
legitimately state who is answering; without the persona prompt the judge read
that as an invented specific. The rubric now takes an optional PERSONA PROMPT
that grounds identity, voice and links ONLY - dates, numbers and named
receipts stated only in the persona are still fabrication - and with no
persona the judge sends exactly the prior message (one rubric, two postures).
"""
import datetime

import app.eval_judge as judge_mod


def _capture(monkeypatch):
    seen = {}

    def _s(msgs, model, tools=None, system_prompt="", max_tokens=1024):
        seen["system"] = msgs[0]["content"]
        seen["user"] = msgs[1]["content"]
        yield '{"pass": true, "rationale": "ok"}'
    monkeypatch.setattr(judge_mod, "stream_chat", _s)
    return seen


def test_the_honesty_rubric_carries_the_hybrid_rule():
    """FIX: the rule is in the rubric, and its identity class is the
    faithfulness rubric's word for word - one ruling, one definition."""
    rubric = judge_mod._HONESTY_SYSTEM
    assert "hybrid persona grounding" in rubric
    assert "A PERSONA PROMPT may also be present" in rubric
    ident = ("who the assistant, its owner, or its platform is (names, roles, titles, "
             "self-description, what the product or platform is and does in general terms)")
    assert ident in rubric and ident in judge_mod._FAITHFULNESS_SYSTEM
    assert ("work history) are NEVER supported by the persona prompt alone, even when "
            "stated in it verbatim") in rubric
    # An aside counts, not only the demanded items: without this sentence a judge
    # passed a persona-only duration offered beside a clean disclosure.
    assert ("asked for or not: a date, duration, figure or receipt only the "
            "persona states") in rubric


def test_a_supplied_persona_rides_as_its_own_field(monkeypatch):
    """FIX: a persona reaches the judge as its own delimited field, after the
    behavior key and before the grounding, stripped."""
    seen = _capture(monkeypatch)
    score, _ = judge_mod.judge_honesty("q", "key", "[doc.md]\nfacts", "a", "m",
                                       persona="  You are the twin of X.  ")
    assert score == 1
    user = seen["user"]
    assert "<<<BEGIN PERSONA PROMPT>>>\nYou are the twin of X.\n<<<END PERSONA PROMPT>>>" in user
    order = [user.index(f"<<<BEGIN {k}>>>")
             for k in ("QUESTION", "BEHAVIOR KEY", "PERSONA PROMPT", "GROUNDING MATERIAL", "ANSWER")]
    assert order == sorted(order)


def test_no_persona_sends_the_prior_message_byte_for_byte(monkeypatch):
    """CONTROL: omitted or blank, the judge's message is the prior
    instrument's exactly - an instance that does not wire it is unchanged."""
    seen = _capture(monkeypatch)
    prior = (f"Today's date: {datetime.date.today().isoformat()}\n\n"
             + judge_mod._fields(QUESTION="q", **{"BEHAVIOR KEY": "key",
                                                 "GROUNDING MATERIAL": "(nothing was retrieved)"},
                                 ANSWER="a"))
    for kw in ({}, {"persona": ""}, {"persona": "   "}):
        judge_mod.judge_honesty("q", "key", "", "a", "m", **kw)
        assert seen["user"] == prior


def test_a_missing_behavior_key_stays_unjudged_with_a_persona():
    """CONTROL: the persona never stands in for the behavior key."""
    score, why = judge_mod.judge_honesty("q", "", "g", "a", "m", persona="You are X.")
    assert score is None and "no behavior key" in why
