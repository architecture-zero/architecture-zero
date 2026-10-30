"""The eval runner sizes its tool room and fits its history as the chat does
(2026-10-29).

The chat sizes a turn's tool budget from the model's window and trims the
oldest history to fit (routers/chat.py _turn_tool_chars,
_fit_history_to_window). The eval runner's tool loop took the whole configured
budget, TurnToolBudget() - 48000 characters - on every window and sent a
multi-turn question's scripted turns whole, so on an 8192-token local window
three full reads overflowed it, the prompt was cut from the FRONT where the
system prompt is, and the eval graded a turn no user would get.

What these pin: on a small window the eval's tool budget is the chat's
window-sized figure for the same turn, the scripted turns are trimmed oldest
first as the chat trims history, and the assembled turn - with the eval's
reply room - fits the window, the system prompt first and whole. CONTROL: a
32768-token window and a lane with no known window send the eval turn as
before: the configured budget and every scripted turn.
"""
import json
import re

import pytest

from app import agent, providers
from app.routers import chat as chat_mod
from tests.test_agent_tool_results_budget import _big_files, _tokens, _ws

BUDGET_OF = re.compile(r"cut at this turn's tool budget of (\d+) characters")
QUESTION = "What do the logs say?"


def _setup_turns(n, size):
    """n alternating scripted turns of `size` characters, numbered."""
    out = []
    for i in range(n):
        head = f"[turn {i:03d}] "
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": head + "x" * (size - len(head))})
    return out


def _eval(monkeypatch, tmp_path, run_id, model, setup):
    """One answer-mode eval question with three tool rounds of full reads;
    the provider doors are stubbed. Returns what the doors were sent."""
    import app.eval_runner as runner
    from app.db import get_session
    from app.models import EvalResult

    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 3)
    sink: dict = {}
    calls = {"n": 0}

    def _tool_call(messages, model, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            sink["first"] = [dict(m) for m in messages]
            sink["tools"] = tools
        return {"message": {"content": "", "tool_calls": [
            {"id": f"t{calls['n']}", "type": "function",
             "function": {"name": "read_file",
                          "arguments": {"path": f"big{calls['n'] - 1}.log"}}}]}}

    def _writer(messages, model, tools=None, system_prompt="", max_tokens=1024):
        sink["last"] = [dict(m) for m in messages]
        sink["max_tokens"] = max_tokens
        yield "an answer"

    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(runner, "supports_tools", lambda model="": True)
    monkeypatch.setattr(runner, "non_stream_tool_call", _tool_call)
    monkeypatch.setattr(runner, "stream_chat", _writer)
    for judge in ("judge_answer", "judge_faithfulness", "judge_freshness"):
        monkeypatch.setattr(f"app.eval_judge.{judge}", lambda *a, **k: (1, "ok"))
    questions = [{"id": 9802, "question": QUESTION, "category": "general",
                  "expected_source": None, "notes": "Reads the logs.", "as_level": None,
                  "setup_turns": json.dumps(setup)}]
    try:
        runner._run_eval_job(run_id, "2026-01-01T00:00:00", questions, model=model,
                             use_rag=False, n_results=5, retrieval_only=False)
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    assert calls["n"] == 3
    return sink


def _kept(sent):
    return [m for m in sent if m.get("role") in ("user", "assistant")
            and str(m.get("content", "")).startswith("[turn")]


def _tool_budget(sent):
    """The eval turn's tool budget, as the tool loop's own cut marker states it."""
    for m in sent:
        if m.get("role") == "tool":
            found = BUDGET_OF.search(m["content"])
            if found:
                return int(found.group(1))
    raise AssertionError("no tool result was cut at the turn's budget")


# -- FIX ----------------------------------------------------------------------

def test_on_a_small_window_the_eval_turn_fits_it(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 8192)
    setup = _setup_turns(40, 400)        # about 4000 tokens of scripted turns
    sink = _eval(monkeypatch, tmp_path, "eval-window-8k", "m", setup)
    first, sent = sink["first"], sink["last"]
    system, prompt = first[0], first[-1]
    # The system prompt first and whole, the question last before the rounds.
    assert system["role"] == "system" and sent[0] == system
    assert prompt == {"role": "user", "content": QUESTION}
    # The scripted turns trimmed oldest first, as the chat trims history.
    kept = _kept(sent)
    assert 0 < len(kept) < len(setup), len(kept)
    assert kept == setup[-len(kept):]
    # The tool budget is the chat's window-sized figure for this turn.
    reply = sink["max_tokens"]
    budget = _tool_budget(sent)
    assert budget == chat_mod._turn_tool_chars(system["content"], prompt["content"],
                                               sink["tools"], reply, "m")
    assert budget < agent.TOOL_RESULTS_TURN_MAX_CHARS
    tool_msgs = [m["content"] for m in sent if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert sum(len(c) for c in tool_msgs if c != agent.TOOL_BUDGET_SPENT) <= budget
    # The assembled turn, with the definitions and the reply's room, fits.
    definitions = len(json.dumps(sink["tools"])) // 4
    assert _tokens(sent) + definitions + reply <= 8192, _tokens(sent)


# -- CONTROL ------------------------------------------------------------------

@pytest.mark.parametrize("model", ["m", "claude-sonnet-4-5-20250929"])
def test_a_large_or_unknown_window_sends_the_eval_turn_as_before(
        monkeypatch, tmp_path, model):
    """A 32768-token local window, and a cloud lane whose window is unknown:
    the configured budget, every scripted turn, the question after them."""
    monkeypatch.setattr(providers, "OLLAMA_NUM_CTX", 32768)
    setup = _setup_turns(40, 400)
    sink = _eval(monkeypatch, tmp_path, f"eval-window-{model}", model, setup)
    first, sent = sink["first"], sink["last"]
    assert first[1:] == [*setup, {"role": "user", "content": QUESTION}]
    assert _kept(sent) == setup
    assert _tool_budget(sent) == agent.TOOL_RESULTS_TURN_MAX_CHARS
    assert sink["max_tokens"] == 1024
