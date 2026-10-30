"""The agent never reads, lists or writes a secret file, whatever the caller's
level (2026-10-29).

The Owner's file tools reach every workspace file (_may_read clears the Owner
everywhere, and None, the internal caller, the same). Where AGENT_WORKSPACE is
a checkout that holds a .env, a TLS key or a credentials file, read_file put
its text into the model's context - on a vendor lane, to the vendor.

What these pin: one deny rule (agent._is_secret), checked before anything is
read or written, for every level, the Owner and None included - the names the
repo's .gitignore marks as secrets (.env, .env.* but not .env.example, *.pem,
*.key) plus ssh private keys (id_rsa* and its siblings) and credentials*.json.
read_file answers a fixed sentence and never opens the file, whether or not it
exists; list_directory and search_files leave the names out; write_file and
create_directory refuse before anything is written. CONTROL: .env.example
reads as before; an ordinary file reads, lists and writes as before; a path
outside the workspace is refused as before.
"""
import pytest

from app import agent
from app.permissions import MEMBER_LEVEL, OWNER_LEVEL

SECRET_TEXT = "PROVIDER_API_KEY=sk-not-a-real-key-0000"

SECRET_NAMES = [
    ".env",
    ".env.production",
    ".env.local",
    "server.pem",
    "tls.key",
    "PROD.PEM",
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    "credentials.json",
    "credentials-prod.json",
    "config/.env",
    ".env.d/extra.txt",
]


def _ws(monkeypatch, tmp_path, write=False):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())
    monkeypatch.setattr(agent, "ENABLE_AGENT_TOOLS", True)
    monkeypatch.setattr(agent, "ENABLE_READ_TOOLS", True)
    monkeypatch.setattr(agent, "ENABLE_WRITE_TOOLS", write)
    return ws


def _put(ws, rel, text=SECRET_TEXT):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _refusal(shown):
    return (f"Permission denied: '{shown}' is a secret file (an environment, "
            f"key or credentials file); the agent never reads, lists or "
            f"writes one.")


def _no_read(monkeypatch):
    """Fail the test if read_file reaches the file at all."""
    def _boom(*a, **k):
        raise AssertionError("read_file opened a secret file")
    monkeypatch.setattr(agent, "_read_file_text", _boom)


# -- FIX ----------------------------------------------------------------------

@pytest.mark.parametrize("level", [OWNER_LEVEL, None])
@pytest.mark.parametrize("name", SECRET_NAMES)
def test_the_owners_read_of_a_secret_file_is_refused_unread(
        monkeypatch, tmp_path, name, level):
    ws = _ws(monkeypatch, tmp_path)
    _put(ws, name)
    _no_read(monkeypatch)
    out = agent.execute_tool("read_file", {"path": name}, user_level=level)
    assert out == _refusal(name), out[:300]
    assert SECRET_TEXT not in out


def test_a_missing_secret_file_gets_the_same_sentence(monkeypatch, tmp_path):
    """No existence oracle: the refusal does not depend on the file being
    there."""
    _ws(monkeypatch, tmp_path)
    _no_read(monkeypatch)
    out = agent.execute_tool("read_file", {"path": ".env"},
                             user_level=OWNER_LEVEL)
    assert out == _refusal(".env"), out


def test_a_lower_level_read_of_a_secret_file_is_refused_too(
        monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    _put(ws, ".env")
    out = agent.execute_tool("read_file", {"path": ".env"},
                             user_level=MEMBER_LEVEL)
    assert out == _refusal(".env"), out
    assert SECRET_TEXT not in out


@pytest.mark.parametrize("level", [OWNER_LEVEL, None])
def test_a_listing_leaves_secret_names_out(monkeypatch, tmp_path, level):
    ws = _ws(monkeypatch, tmp_path)
    for name in (".env", ".env.production", "server.pem", "tls.key",
                 "id_rsa", "credentials.json", ".env.example", "README.md"):
        _put(ws, name, "x")
    (ws / ".env.d").mkdir()
    (ws / "src").mkdir()
    out = agent.execute_tool("list_directory", {"path": "."},
                             user_level=level)
    assert out.splitlines() == ["[dir]  src", "[file] .env.example",
                                "[file] README.md"], out


def test_listing_a_secret_directory_is_refused(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    _put(ws, ".env.d/extra.txt")
    out = agent.execute_tool("list_directory", {"path": ".env.d"},
                             user_level=OWNER_LEVEL)
    assert out == _refusal(".env.d"), out


@pytest.mark.parametrize("level", [OWNER_LEVEL, None])
def test_a_search_leaves_secret_names_out(monkeypatch, tmp_path, level):
    ws = _ws(monkeypatch, tmp_path)
    for name in (".env", "config/.env", "keys/tls.key", "certs/server.pem",
                 "credentials.json", ".env.example", "config/app.json"):
        _put(ws, name, "x")
    def _found(pattern):
        out = agent.execute_tool("search_files", {"pattern": pattern},
                                 user_level=level)
        return {line.replace("\\", "/") for line in out.splitlines()}
    # glob's "*" skips dot names, so the dot names get their own pattern.
    assert _found("**/*") == {"config", "config/app.json", "keys", "certs"}
    assert _found("**/.env*") == {".env.example"}


@pytest.mark.parametrize("level", [OWNER_LEVEL, None])
@pytest.mark.parametrize("name", [".env", ".env.production", "tls.key",
                                  "config/credentials.json"])
def test_a_write_to_a_secret_file_is_refused_unwritten(
        monkeypatch, tmp_path, name, level):
    ws = _ws(monkeypatch, tmp_path, write=True)
    target = ws / name
    out = agent.execute_tool("write_file", {"path": name, "content": "X=1"},
                             user_level=level)
    assert out == _refusal(name), out
    assert not target.exists()


def test_an_existing_secret_file_is_not_overwritten(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path, write=True)
    p = _put(ws, ".env")
    out = agent.execute_tool("write_file", {"path": ".env", "content": "X=1"},
                             user_level=OWNER_LEVEL)
    assert out == _refusal(".env"), out
    assert p.read_text(encoding="utf-8") == SECRET_TEXT


@pytest.mark.parametrize("level", [OWNER_LEVEL, None])
def test_creating_a_secret_directory_is_refused(monkeypatch, tmp_path, level):
    ws = _ws(monkeypatch, tmp_path, write=True)
    out = agent.execute_tool("create_directory", {"path": ".env.d"},
                             user_level=level)
    assert out == _refusal(".env.d"), out
    assert not (ws / ".env.d").exists()


# -- CONTROL ------------------------------------------------------------------

@pytest.mark.parametrize("level", [OWNER_LEVEL, MEMBER_LEVEL, None])
def test_the_env_example_reads_as_before(monkeypatch, tmp_path, level):
    ws = _ws(monkeypatch, tmp_path)
    _put(ws, ".env.example", "PROVIDER_API_KEY=")
    out = agent.execute_tool("read_file", {"path": ".env.example"},
                             user_level=level)
    assert out.endswith("PROVIDER_API_KEY="), out[:300]
    assert "Permission denied" not in out


def test_an_ordinary_file_reads_as_before(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    _put(ws, "notes/keys-and-envs.md", "how the .env and the key files work")
    out = agent.execute_tool("read_file", {"path": "notes/keys-and-envs.md"},
                             user_level=OWNER_LEVEL)
    assert out.endswith("how the .env and the key files work"), out[:300]


def test_an_ordinary_write_and_directory_work_as_before(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path, write=True)
    assert agent.execute_tool("write_file", {"path": "out/a.txt",
                                             "content": "hi"},
                              user_level=OWNER_LEVEL) == "Written: out/a.txt"
    assert (ws / "out" / "a.txt").read_text(encoding="utf-8") == "hi"
    assert agent.execute_tool("create_directory", {"path": "envs"},
                              user_level=OWNER_LEVEL) == "Created: envs"
    assert (ws / "envs").is_dir()


def test_a_path_outside_the_workspace_is_refused_as_before(
        monkeypatch, tmp_path):
    _ws(monkeypatch, tmp_path)
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    out = agent.execute_tool("read_file", {"path": "../outside.txt"},
                             user_level=OWNER_LEVEL)
    assert out == ("Permission denied: Path '../outside.txt' escapes the "
                   "agent workspace"), out
