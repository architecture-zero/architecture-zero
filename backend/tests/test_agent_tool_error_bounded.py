"""A failed agent tool hands the model an exception's words typed and bounded
at 300 (2026-10-31).

agent.execute_tool's last two handlers returned f"Permission denied: {e}" and
f"Error executing {name}: {e}" - the exception's words whole, as the tool
result the model reads and may repeat in its answer: a library's error
body, a file system's path, a decoder's bytes from the file it could not
read. Each is bounded now: the PermissionError's words cut at 300 after
"Permission denied: " (the prefix names the type, and the workspace guard's
own sentence reads as before), any other failure's
f"{type(e).__name__}: {e}"[:300] after "Error executing <tool>: ".
CONTROL: the workspace guard's refusal reads as the model has always read
it; a timeout reads as before with its type first
(test_agent_run_command_reads_its_cap.py pins that one).
"""
import pytest

from app import agent

_LONG = "the library's words at /data/private/path " + "x" * 2000


@pytest.fixture
def tools(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", tmp_path.resolve())
    monkeypatch.setattr(agent, "ENABLE_AGENT_TOOLS", True)
    monkeypatch.setattr(agent, "ENABLE_READ_TOOLS", True)


def _raising(exc):
    def _boom(path):
        raise exc
    return _boom


def test_a_failed_tool_hands_the_model_its_error_typed_and_bounded(tools, monkeypatch):
    monkeypatch.setattr(agent, "_safe_path", _raising(RuntimeError(_LONG)))
    out = agent.execute_tool("read_file", {"path": "a.md"})
    prefix = "Error executing read_file: "
    assert out == prefix + f"RuntimeError: {_LONG}"[:300], out[:120]
    assert len(out) <= len(prefix) + 300


def test_a_permission_error_is_bounded_after_its_own_prefix(tools, monkeypatch):
    monkeypatch.setattr(agent, "_safe_path", _raising(PermissionError(_LONG)))
    out = agent.execute_tool("read_file", {"path": "a.md"})
    assert out == "Permission denied: " + _LONG[:300], out[:120]


def test_control_the_workspace_guards_refusal_reads_as_before(tools):
    out = agent.execute_tool("read_file", {"path": "../outside.md"})
    assert out == "Permission denied: Path '../outside.md' escapes the agent workspace"
