"""The follow-up matcher reads only a short message (2026-09-30).

Its pattern's two whitespace runs around the punctuation went quadratic on a
long run of spaces - 1.09 s at 24,000 characters - and the chat bound lets a
signed-in caller send far more. A follow-up is a few words, so the length is
checked before the pattern runs.
"""
import time

from app.routing import FOLLOWUP_MAX_CHARS, is_followup


def test_a_follow_up_is_still_a_follow_up():
    for q in ("now", "  latest?  ", "what's next", "tell me more!", "Where are we?"):
        assert is_followup(q), q


def test_a_long_message_is_never_one_and_costs_nothing():
    assert not is_followup("x" * (FOLLOWUP_MAX_CHARS + 1))
    t0 = time.monotonic()
    assert not is_followup("now" + " " * 200000 + "x")
    assert time.monotonic() - t0 < 0.1
