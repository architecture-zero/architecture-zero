"""The agent-tool flag gate: execute_tool derives an active set from the SAME
source the offer uses and refuses names outside it, so the ENABLE_* flags
gate EXECUTION, not just what the model is offered. Plus the smaller half:
run_command is Owner-only and create_directory applies the same clearance
rule as write_file. And search_files keeps a match only when it resolves
inside the workspace.

The exact-contents pins matter: a new tool added to any list, or a new elif
added to the dispatcher without its flag, must turn something here red."""
from app import agent
from app.permissions import ADMIN_LEVEL, MEMBER_LEVEL, OWNER_LEVEL

FULL_SET = {"read_file", "list_directory", "search_files",
            "write_file", "create_directory", "run_command"}


def _names():
    return {t["function"]["name"] for t in agent.get_active_tools()}


def _flags(monkeypatch, master, read=False, write=False, shell=False):
    monkeypatch.setattr(agent, "ENABLE_AGENT_TOOLS", master)
    monkeypatch.setattr(agent, "ENABLE_READ_TOOLS", read)
    monkeypatch.setattr(agent, "ENABLE_WRITE_TOOLS", write)
    monkeypatch.setattr(agent, "ENABLE_SHELL_TOOLS", shell)


def test_active_set_exact_contents_per_flag_combo(monkeypatch):
    _flags(monkeypatch, master=False, read=True, write=True, shell=True)
    assert _names() == set()   # master off beats every sub-flag

    _flags(monkeypatch, master=True)
    assert _names() == set()

    _flags(monkeypatch, master=True, read=True)
    assert _names() == {"read_file", "list_directory", "search_files"}

    _flags(monkeypatch, master=True, read=True, write=True)
    assert _names() == FULL_SET - {"run_command"}

    _flags(monkeypatch, master=True, read=True, write=True, shell=True)
    assert _names() == FULL_SET


def test_execution_gated_not_just_advertisement(monkeypatch, tmp_path):
    """A KNOWN name arriving by any path other than the offered list used to
    execute regardless of flag state."""
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", tmp_path.resolve())
    _flags(monkeypatch, master=False)
    for name, args in (("run_command", {"command": "echo hi"}),
                       ("write_file", {"path": "x.txt", "content": "x"}),
                       ("read_file", {"path": "x.txt"}),
                       ("create_directory", {"path": "d"})):
        out = agent.execute_tool(name, args)
        assert "not enabled" in out, (name, out)
    assert not (tmp_path / "x.txt").exists()
    assert not (tmp_path / "d").exists()

    # Master on but shell off: the shell name specifically stays refused,
    # for the Owner too.
    _flags(monkeypatch, master=True, read=True, write=True, shell=False)
    assert "not enabled" in agent.execute_tool("run_command",
                                               {"command": "echo hi"})
    assert "not enabled" in agent.execute_tool("run_command",
                                               {"command": "echo hi"},
                                               user_level=OWNER_LEVEL)


def test_unknown_names_refused_by_the_same_gate(monkeypatch):
    _flags(monkeypatch, master=True, read=True, write=True, shell=True)
    assert "not enabled" in agent.execute_tool("drop_database", {})


def test_run_command_is_owner_only(monkeypatch, tmp_path):
    _flags(monkeypatch, master=True, shell=True)
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", tmp_path.resolve())
    for level in (MEMBER_LEVEL, ADMIN_LEVEL):
        out = agent.execute_tool("run_command", {"command": "echo hi"},
                                 user_level=level)
        assert "Owner-only" in out, (level, out)


def test_run_command_executes_for_owner(monkeypatch, tmp_path):
    """The Owner's rung, as chat passes it, and the internal default (None)."""
    _flags(monkeypatch, master=True, shell=True)
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", tmp_path.resolve())
    for level in (OWNER_LEVEL, None):
        out = agent.execute_tool("run_command", {"command": "echo hi"},
                                 user_level=level)
        assert "hi" in out and "Owner-only" not in out, (level, out)


def test_create_directory_applies_clearance(monkeypatch, tmp_path):
    _flags(monkeypatch, master=True, write=True)
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", tmp_path.resolve())
    monkeypatch.setattr(agent, "_may_read", lambda p, level: level is None)
    out = agent.execute_tool("create_directory", {"path": "newdir"},
                             user_level=MEMBER_LEVEL)
    assert "Permission denied" in out
    assert not (tmp_path / "newdir").exists()
    out = agent.execute_tool("create_directory", {"path": "newdir"},
                             user_level=None)
    assert "Created" in out
    assert (tmp_path / "newdir").is_dir()


# -- search_files stays inside the workspace ----------------------------------

def _search_layout(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "inside.md").write_text("in", encoding="utf-8")
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "outside-name.txt").write_text("out", encoding="utf-8")
    # A sibling that shares the workspace's prefix is still outside it.
    (tmp_path / "ws-evil").mkdir()
    (tmp_path / "ws-evil" / "sibling-name.txt").write_text("out", encoding="utf-8")
    return ws


def test_search_files_never_lists_an_outside_name(monkeypatch, tmp_path):
    """A "../" pattern used to list outside names as ordinary hits (relative_to
    is lexical), and an absolute pattern, or any outside match for a caller
    below Owner, raised an error whose text named the outside path."""
    ws = _search_layout(tmp_path)
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())
    _flags(monkeypatch, master=True, read=True)
    patterns = ("../secret/*", "../ws-evil/*", "../**/*.txt",
                str(tmp_path / "secret" / "*"))
    for level in (None, OWNER_LEVEL, MEMBER_LEVEL):
        for pattern in patterns:
            out = agent.execute_tool("search_files", {"pattern": pattern},
                                     user_level=level)
            assert "outside-name" not in out, (level, pattern, out)
            assert "sibling-name" not in out, (level, pattern, out)
            assert str(tmp_path) not in out, (level, pattern, out)
            assert out == "No files found matching pattern", (level, pattern, out)


def test_search_files_still_finds_inside_matches(monkeypatch, tmp_path):
    ws = _search_layout(tmp_path)
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())
    _flags(monkeypatch, master=True, read=True)
    assert agent.execute_tool("search_files", {"pattern": "**/*.md"}) == "inside.md"
    # A pattern that steps out and back in still lands inside.
    assert agent.execute_tool("search_files",
                              {"pattern": "../ws/*.md"}) == "inside.md"
