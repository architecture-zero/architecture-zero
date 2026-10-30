"""A turn's tool results share one budget (2026-10-29).

read_file caps ONE result at READ_FILE_MAX_CHARS (24000 characters at the
default), but the chat runs up to five tool rounds and the eval runner three,
and both appended every result whole: five capped reads are about 30000
tokens, past a 32768-token local Ollama window once the reply, the history
budget and the system prompt are counted - and Ollama cuts an over-long
prompt from the FRONT, where the system prompt and its safety rules are.

What these pin: the tool results one turn appends total at most
agent.TOOL_RESULTS_TURN_MAX_CHARS, the result that crosses it is cut with a
marker, a later one is the fixed spent sentence, and what is sent still fits
the smallest window with the system prompt first and whole - in the chat and
in the eval runner. CONTROL: one small read is appended exactly as
execute_tool returned it.
"""
from unittest.mock import patch

from app import agent
from app.providers import OLLAMA_NUM_CTX

REPLY_TOKENS = 4096   # a signed-in turn's max_tokens; it shares the window
CUT = "[tool results: cut at this turn's tool budget of"


def _ws(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())
    monkeypatch.setattr(agent, "ENABLE_AGENT_TOOLS", True)
    monkeypatch.setattr(agent, "ENABLE_READ_TOOLS", True)
    return ws


def _big_files(ws, n):
    """n files each longer than one read's cap, so every read is a full one."""
    body = "".join(f"line {i:06d}\n" for i in range(agent.READ_FILE_MAX_CHARS // 12 + 500))
    assert len(body) > agent.READ_FILE_MAX_CHARS
    for i in range(n):
        (ws / f"big{i}.log").write_text(body, encoding="utf-8")


def _tokens(messages):
    """The chat's own estimate (four characters a token), over everything
    sent: contents and the tool calls the assistant turns carry."""
    return sum(len(str(m.get("content", ""))) + len(str(m.get("tool_calls", "")))
               for m in messages) // 4


def _assert_bounded(messages, first_system):
    tool_msgs = [m["content"] for m in messages if m.get("role") == "tool"]
    # What is sent fits the smallest window with the reply's room left, and
    # the system prompt rides first and whole.
    assert _tokens(messages) + REPLY_TOKENS <= OLLAMA_NUM_CTX, (
        _tokens(messages), [len(c) for c in tool_msgs])
    assert messages[0] == first_system
    assert any(CUT in c for c in tool_msgs), [c[-160:] for c in tool_msgs]
    counted = [c for c in tool_msgs if c != agent.TOOL_BUDGET_SPENT]
    assert sum(len(c) for c in counted) <= agent.TOOL_RESULTS_TURN_MAX_CHARS
    assert agent.TOOL_BUDGET_SPENT in tool_msgs
    return tool_msgs


# -- the chat route --------------------------------------------------------------

def _chat(client, admin_headers, rounds, path_for, sink):
    calls = {"n": 0}

    def _stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
        calls["n"] += 1
        if calls["n"] == 1:
            sink["first_system"] = dict(messages[0])
        if calls["n"] <= rounds:
            yield {"type": "tool_call", "id": f"t{calls['n']}", "name": "read_file",
                   "args": {"path": path_for(calls["n"])}}
            return
        sink["messages"] = [dict(m) for m in messages]
        yield {"type": "text", "text": "done"}

    with patch("app.routers.chat.stream_chat_events", side_effect=_stream):
        r = client.post("/api/chat", headers=admin_headers,
                        json={"prompt": "Read the logs.", "model": "test-model",
                              "use_rag": False, "session_id": f"s-tool-budget-{rounds}"})
    assert r.status_code == 200, r.text
    assert "done" in r.text
    return sink


def test_five_rounds_of_full_reads_stay_inside_the_turn_budget(
        client, admin_headers, monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    _big_files(ws, 5)
    sink = _chat(client, admin_headers, 5, lambda n: f"big{n - 1}.log", {})
    tool_msgs = _assert_bounded(sink["messages"], sink["first_system"])
    assert len(tool_msgs) == 5
    # The first read fits whole; the second is the one cut.
    assert tool_msgs[0] == agent.execute_tool("read_file", {"path": "big0.log"})


def test_one_small_read_is_appended_exactly_as_before(
        client, admin_headers, monkeypatch, tmp_path):
    """CONTROL - passes before the change too."""
    ws = _ws(monkeypatch, tmp_path)
    (ws / "small.md").write_text("# Small\nOne line of notes.\n", encoding="utf-8")
    sink = _chat(client, admin_headers, 1, lambda n: "small.md", {})
    tool_msgs = [m["content"] for m in sink["messages"] if m.get("role") == "tool"]
    assert tool_msgs == [agent.execute_tool("read_file", {"path": "small.md"})]


# -- the eval runner -------------------------------------------------------------

def test_the_eval_runner_holds_the_same_budget(client, monkeypatch, tmp_path):
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
            sink["first_system"] = dict(messages[0])
        return {"message": {"content": "", "tool_calls": [
            {"id": f"t{calls['n']}", "type": "function",
             "function": {"name": "read_file",
                          "arguments": {"path": f"big{calls['n'] - 1}.log"}}}]}}

    def _writer(messages, model, tools=None, system_prompt="", max_tokens=1024):
        sink["messages"] = [dict(m) for m in messages]
        yield "an answer"

    monkeypatch.setattr("app.database.corpus_fingerprint", lambda: "src=0;chunks=0;sha=0")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(runner, "supports_tools", lambda model="": True)
    monkeypatch.setattr(runner, "non_stream_tool_call", _tool_call)
    monkeypatch.setattr(runner, "stream_chat", _writer)
    for judge in ("judge_answer", "judge_faithfulness", "judge_freshness"):
        monkeypatch.setattr(f"app.eval_judge.{judge}", lambda *a, **k: (1, "ok"))
    run_id = "tool-budget-eval"
    questions = [{"id": 9801, "question": "What do the logs say?", "category": "general",
                  "expected_source": None, "notes": "Reads the logs.", "as_level": None}]
    try:
        runner._run_eval_job(run_id, "2026-01-01T00:00:00", questions, model="m",
                             use_rag=False, n_results=5, retrieval_only=False)
    finally:
        runner._eval_runs.pop(run_id, None)
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.run_id == run_id).delete()
    assert calls["n"] == 3
    tool_msgs = _assert_bounded(sink["messages"], sink["first_system"])
    assert len(tool_msgs) == 3


# -- the budget itself -------------------------------------------------------------

def test_the_budget_cuts_inside_itself_then_says_it_is_spent():
    b = agent.TurnToolBudget(400)
    assert b.fit("a" * 100) == "a" * 100
    cut = b.fit("b" * 400)
    assert cut.startswith("b") and CUT in cut
    assert len(cut) == 300
    assert b.fit("c") == agent.TOOL_BUDGET_SPENT
    # A result that fits exactly is whole.
    exact = agent.TurnToolBudget(10)
    assert exact.fit("0123456789") == "0123456789"
