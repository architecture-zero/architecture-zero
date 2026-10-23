"""The helpful tail after a refusal (found by the forks' eval runs, 2026-10-22).

On the 5.5 writer a correct refusal of an artifact the corpus does not hold then
added "what the context does cover" with a path the context never named (a
deployment's eval: "The system's own roadmap is docs/ROADMAP.md, but its
contents weren't retrieved here") or dated receipts from outside the retrieved
context. The grounding rules gain one rule: when the context does not hold what
was asked, say so, then stop or offer only what the context contains - never a
file, path or document title nothing in the turn names. The unit suite proves
the rule REACHES both prompts; only a live honesty run proves it works.
"""
import inspect

from app.runtime_config import _GROUNDING_RULES


def test_the_refusal_tail_rule_is_in_the_grounding_rules():
    """FIX: the rule is in the block every chat turn and every eval question carries."""
    assert "When the context does not hold what was asked" in _GROUNDING_RULES
    assert "offer only what the retrieved context itself" in _GROUNDING_RULES
    assert "Never name a file, path, or document title" in _GROUNDING_RULES


def test_the_existing_figure_rule_is_kept():
    """CONTROL: the figure rule beside it stays in the block."""
    assert "Never state a specific figure" in _GROUNDING_RULES
    assert "Do not estimate, infer, round, or fill numbers" in _GROUNDING_RULES


def test_both_prompts_carry_the_grounding_rules():
    """CONTROL: the chat and the eval both append the block (the rule rides it)."""
    from app import eval_runner
    from app.routers import chat
    assert "_GROUNDING_RULES" in inspect.getsource(chat)
    assert "_GROUNDING_RULES" in inspect.getsource(eval_runner)
