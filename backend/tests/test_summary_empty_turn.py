"""The history summarizer and the provider's empty-turn sentence (2026-10-22).

stream_chat_events and the non-streaming door answer an empty turn with one plain
sentence (providers.EMPTY_TURN_TEXT) so no lane serves silence. The summarizer
reads that door, and the sentence is an answer, not a summary: an empty summarizer
completion must still take the default summary, as it did before the exit started
answering. FIX: the sentence -> the default. CONTROL: a real summary passes through.
"""
from types import SimpleNamespace
from unittest.mock import patch

# The summarizer reads stored message rows (.role / .content), not request dicts.
_TURNS = [SimpleNamespace(role="user", content="What did we decide about the rollout?"),
          SimpleNamespace(role="assistant", content="One surface at a time, each with its gate.")]
_DEFAULT = "Previous conversation was summarized."


def test_the_empty_turn_sentence_is_not_a_summary():
    from app.providers import EMPTY_TURN_TEXT
    from app.routers.chat import _summarize_history
    with patch("app.routers.chat.non_stream_tool_call",
               return_value={"message": {"content": EMPTY_TURN_TEXT, "tool_calls": []}}):
        assert _summarize_history(_TURNS, "m") == _DEFAULT


def test_an_empty_summary_still_takes_the_default():
    from app.routers.chat import _summarize_history
    with patch("app.routers.chat.non_stream_tool_call",
               return_value={"message": {"content": "", "tool_calls": []}}):
        assert _summarize_history(_TURNS, "m") == _DEFAULT


def test_a_real_summary_passes_through():
    from app.routers.chat import _summarize_history
    with patch("app.routers.chat.non_stream_tool_call",
               return_value={"message": {"content": "They agreed to roll out one surface at a time.",
                                         "tool_calls": []}}):
        assert _summarize_history(_TURNS, "m") == "They agreed to roll out one surface at a time."
