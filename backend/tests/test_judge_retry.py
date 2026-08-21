"""Judge-retry de-coercion pin (outside-review finding).

The retry on an unparseable verdict used to forbid the judge from declining -
format enforcement fused with evaluative pressure, which manufactures a
verdict exactly when the judge emitted prose because the case was genuinely
unjudgeable. The retry now enforces the JSON schema only; a second prose
reply lands unscored (None), the outcome the module's contract reserves for
judge failures. Validated by a full planted-suite re-run on the pinned judge
after the change.
"""
from pathlib import Path

from app.eval_judge import _run_judge


def test_judge_retry_has_no_coercion_clause():
    """Re-open condition: the retry may enforce FORMAT, never a verdict."""
    src = (Path(__file__).resolve().parents[1] / "app" / "eval_judge.py").read_text(
        encoding="utf-8")
    assert "do not decline" not in src
    assert "You have enough information" not in src
    assert '{"pass": true|false, "rationale": "..."}' in src


def test_run_judge_prose_twice_stays_unscored(monkeypatch):
    """A judge that declines twice lands unscored - never a manufactured verdict."""
    import app.eval_judge as ej

    def prose_chat(msgs, model, system_prompt=None, max_tokens=None):
        yield "I cannot responsibly grade this case."

    monkeypatch.setattr(ej, "stream_chat", prose_chat)
    score, rationale = _run_judge("sys", "user", "model-x")
    assert score is None
    assert "unparseable" in rationale


def test_run_judge_recovers_on_formatted_retry(monkeypatch):
    import app.eval_judge as ej
    calls = {"n": 0}

    def flaky_chat(msgs, model, system_prompt=None, max_tokens=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield "Sure - here is my thinking, in prose."
        else:
            yield '{"pass": true, "rationale": "matches the key"}'

    monkeypatch.setattr(ej, "stream_chat", flaky_chat)
    score, rationale = _run_judge("sys", "user", "model-x")
    assert score == 1
    assert rationale == "matches the key"
