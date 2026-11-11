"""REQUIRE_MFA says what it does (2026-11-11).

REQUIRE_MFA is the host's switch: on, a password sign-in on an account with no
enrolled authenticator is refused, and an unclaimed deployment cannot be
claimed. The refusals themselves are pinned in test_security_brick.py and
test_setup_claim_code.py. This file is about the switch.

THE GAP. Only the exact word `true` turned it on, and every other value read
as off without a word. An operator wrote REQUIRE_MFA=1, restarted, ticked the
item, and accounts with no authenticator went on signing in with a password
alone - a control that reads as configured and performs nothing. Such a value
now refuses to start (routers/auth.py, _require_mfa_from_env): the message
names the variable and the two words it takes and never the value. `true` and
`false` in any letter case, an empty value and an unset variable read exactly
as they did. And every boot that does start says which way the switch was
read, in one log line.

THREE THINGS THAT WERE TRUE AND PINNED BY NOTHING:
  - the ENVIRONMENT VARIABLE drives the constant. Every other test of this
    flag sets the module attribute, so a misspelt name inside the read would
    leave them all green with the flag dead. That is this flag's own history:
    it was once read and consulted by nothing.
  - nothing else under app/ reads it. The refusals were pinned by behaviour
    only; a second reader - a route that reports the posture, a door with a
    copy of its own - would have passed every test.
  - the account lock comes first. Under the flag a locked account still gets
    the lock's 401 for a correct password, and the flag's 403 moves no lockout
    bookkeeping in either direction.

Each test says whether it is a FIX (it fails on the code from before the
strict read) or a CONTROL (it holds on both, because what it pins is what
must not move).
"""
import ast
import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.jwt_auth import hash_password
from app.routers import auth as auth_router
from app.users import (create_user, get_user_by_id, lock_user,
                       reset_failed_attempts, unlock_user)

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
REPO = BACKEND.parent
HOME = "routers/auth.py"            # the one module that may name the flag

ON = ["true", "TRUE", "True", "tRuE"]
OFF = ["false", "FALSE", "False", ""]
# What an operator types believing it means "on" or "off", and what a careless
# env file leaves around the word. None of these is one of the two words.
REFUSED = ["1", "0", "yes", "no", "on", "off", "y", "n", "t", "f", "enabled",
           " true", "true ", "\ttrue", "true\n", "true\r", " false", "false ",
           '"true"', "'true'", "true # set by ops", "truee", "tru", "true,false",
           " ", "none", "null"]
# A value nobody would type, to prove the refusal does not quote what it read.
MARKER = "Zq7-pasted-by-mistake"


# -- the strict read ----------------------------------------------------------
#
# No value that was recognised changes meaning, in either direction: what read
# as on is on, what was written as off is off. The line the strict reader
# replaced is kept here as the yardstick, so each test states its "before"
# beside its "now". (The first three fail on the old code only because the
# reader had no name there; the same values are asked of the old code itself
# in the fresh-interpreter tests below, which hold on both.)

def _the_line_this_replaced() -> bool:
    return os.getenv("REQUIRE_MFA", "false").lower() == "true"


@pytest.mark.parametrize("value", ON)
def test_only_the_word_true_turns_it_on(monkeypatch, value):
    monkeypatch.setenv("REQUIRE_MFA", value)
    assert _the_line_this_replaced() is True                 # on before
    assert auth_router._require_mfa_from_env() is True       # on now


@pytest.mark.parametrize("value", OFF)
def test_the_word_false_or_an_empty_value_leaves_it_off(monkeypatch, value):
    monkeypatch.setenv("REQUIRE_MFA", value)
    assert _the_line_this_replaced() is False                # off before
    assert auth_router._require_mfa_from_env() is False      # off now


def test_an_unset_variable_leaves_it_off(monkeypatch):
    monkeypatch.delenv("REQUIRE_MFA", raising=False)
    assert _the_line_this_replaced() is False
    assert auth_router._require_mfa_from_env() is False


@pytest.mark.parametrize("value", REFUSED, ids=[repr(v) for v in REFUSED])
def test_any_other_value_refuses_to_start(monkeypatch, value):
    """FIX. Every one of these read as off, silently. None is accepted as on
    either: reading `1` as on would switch enforcement on at an update, on a
    host whose accounts never enrolled. A padded word is refused, not trimmed."""
    monkeypatch.setenv("REQUIRE_MFA", value)
    assert _the_line_this_replaced() is False                # it read as off
    with pytest.raises(RuntimeError) as refusal:
        auth_router._require_mfa_from_env()
    message = str(refusal.value)
    assert message.startswith("SECURITY: REQUIRE_MFA ")
    # the variable, the two words it takes, and what an unset one means
    assert "REQUIRE_MFA=true" in message and "REQUIRE_MFA=false" in message
    assert "unset or empty is off" in message


def test_the_refusal_names_the_variable_and_never_the_value(monkeypatch):
    """FIX. An env file is not something a boot log should quote from: a value
    pasted onto the wrong line may be a secret."""
    monkeypatch.setenv("REQUIRE_MFA", MARKER)
    with pytest.raises(RuntimeError) as refusal:
        auth_router._require_mfa_from_env()
    assert MARKER.lower() not in str(refusal.value).lower()
    assert MARKER.lower() not in repr(refusal.value).lower()


# -- the environment variable drives the constant -----------------------------
#
# Tested at import, in a fresh interpreter, because import is where the read
# happens: the constant the two routes consult is bound once, when the router
# module loads. The child gets its own database file and log directory, so it
# never opens the running suite's. And it opens no vector store: the router's
# imports do not reach app.database today, but if they ever do, `import
# chromadb` finds a stand-in there, as conftest stands one in for the client.

_PROBE = "\n".join((
    "import json, sys",
    "from unittest.mock import MagicMock",
    "sys.modules['chromadb'] = MagicMock(name='chromadb')",
    "import app.routers.auth as a",
    "print('REQUIRE_MFA=' + json.dumps(a.REQUIRE_MFA))",
))


def _fresh_import(tmp_path, value, code=_PROBE):
    """Run `code` - by default, import the auth router - in a new interpreter
    with REQUIRE_MFA set to `value` (None leaves it unset); the finished
    process."""
    env = {k: v for k, v in os.environ.items() if k != "REQUIRE_MFA"}
    env["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'fresh-import.db').as_posix()}"
    env["LOG_DIR"] = str(tmp_path)
    if value is not None:
        env["REQUIRE_MFA"] = value
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, cwd=str(BACKEND), timeout=300)


@pytest.mark.parametrize("value, expected", [
    ("true", "true"), ("TRUE", "true"), ("false", "false"), ("", "false"),
    (None, "false")], ids=["true", "TRUE", "false", "empty", "unset"])
def test_the_environment_variable_is_what_the_routes_constant_holds(
        tmp_path, value, expected):
    """CONTROL: holds on the code from before the strict read too. What it
    pins is what every other test of this flag assumes - a host that writes
    REQUIRE_MFA=true gets the constant the login and claim routes read set
    to True. Every test outside this file sets the module attribute, so with
    the variable's name misspelt inside the read they all stay green; the
    `true` legs here do not."""
    done = _fresh_import(tmp_path, value)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == f"REQUIRE_MFA={expected}"


@pytest.mark.parametrize("value", ["1", " true", MARKER],
                         ids=["1", "padded_true", "marker"])
def test_an_unrecognised_value_stops_the_import(tmp_path, value):
    """FIX. The refusal is at import, so the app is never built: on the old
    code each of these imported cleanly with the flag off."""
    done = _fresh_import(tmp_path, value)
    assert done.returncode != 0, done.stdout
    assert "REQUIRE_MFA=" not in done.stdout          # the constant was never bound
    last = done.stderr.strip().splitlines()[-1]
    assert last.startswith("RuntimeError: SECURITY: REQUIRE_MFA "), done.stderr
    assert "REQUIRE_MFA=true or REQUIRE_MFA=false" in last
    # nowhere in what the process printed, the traceback included
    assert MARKER.lower() not in (done.stdout + done.stderr).lower()


# The whole app this time, as a boot imports it, against a database file that
# does not exist yet. A boot that is going to refuse must refuse before it has
# created, seeded or purged anything, which holds because main imports its
# routers ahead of its schema step (main.py says so where it does). The count
# of tables the refused boot left behind is the proof.
_BOOT = "\n".join((
    "import os, sqlite3, sys",
    "from unittest.mock import MagicMock",
    "sys.modules['chromadb'] = MagicMock(name='chromadb')",
    "try:",
    "    import app.main",
    "except RuntimeError as refusal:",
    "    path = os.environ['DATABASE_URL'].replace('sqlite:///', '')",
    "    made = 0",
    "    if os.path.exists(path) and os.path.getsize(path):",
    "        made = sqlite3.connect(path).execute(",
    "            \"select count(*) from sqlite_master where type = 'table'\").fetchone()[0]",
    "    print('REFUSED tables=%d' % made)",
    "    print(str(refusal))",
    "else:",
    "    print('BOOTED')",
    "sys.stdout.flush()",
    "os._exit(0)",          # what was imported is of no interest: skip its teardown
))


def test_the_refusal_comes_before_any_boot_step(tmp_path):
    """FIX, in that nothing refused before. The ORDER it pins was already
    this file's: with the routers imported below the schema step instead, a
    refused boot would already have made the tables, seeded the config and
    run the audit purge - on a deployment that then never starts."""
    done = _fresh_import(tmp_path, "1", _BOOT)
    assert done.returncode == 0, done.stderr
    said = done.stdout.strip().splitlines()
    assert said[-2:-1] == ["REFUSED tables=0"], done.stdout + done.stderr
    assert said[-1].startswith("SECURITY: REQUIRE_MFA ")


# -- one read, one home, three readers ----------------------------------------

def _router_tree():
    return ast.parse((APP / HOME).read_text(encoding="utf-8"))


def _files_naming_the_flag(app_dir: Path) -> list[str]:
    return sorted(p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*.py")
                  if "__pycache__" not in p.parts
                  and "REQUIRE_MFA" in p.read_text(encoding="utf-8"))


def test_no_other_module_under_app_names_the_flag():
    """CONTROL. The string, not just the read: a comment that names the flag
    in a second module is where a second reader starts. A module that needs
    the state asks the auth router; the boot's line does (main calls
    log_require_mfa and never touches the name)."""
    assert _files_naming_the_flag(APP) == [HOME]


def test_the_scan_can_see_a_second_module(tmp_path):
    """The control for the test above: a guard that cannot fail is not one."""
    (tmp_path / "routers").mkdir()
    (tmp_path / "routers" / "auth.py").write_text(
        'REQUIRE_MFA = _require_mfa_from_env()\n', encoding="utf-8")
    (tmp_path / "door.py").write_text(
        'import os\nif os.getenv("REQUIRE_MFA") == "true":\n    pass\n', encoding="utf-8")
    (tmp_path / "quiet.py").write_text("# nothing to see\n", encoding="utf-8")
    assert _files_naming_the_flag(tmp_path) == ["door.py", HOME]


def test_the_constant_is_the_strict_readers_answer_and_the_only_read():
    """FIX. One assignment at module level, bound to the strict reader's
    return, and one place where the environment is asked for the name - so a
    second read beside it (a route asking the environment at call time) is not
    a way round the refusal."""
    tree = _router_tree()
    assigned = [node for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "REQUIRE_MFA"
                        for t in node.targets)]
    assert len(assigned) == 1, [node.lineno for node in assigned]
    value = assigned[0].value
    assert (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "_require_mfa_from_env"
            and not value.args and not value.keywords), ast.dump(value)
    asked = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and node.value == "REQUIRE_MFA"]
    assert len(asked) == 1, asked


def test_the_flag_has_three_readers_and_none_answers_a_caller_with_no_session():
    """FIX for the third name, CONTROL for the first two. The login route and
    the claim route are the two that enforce it, and each answers only after a
    password or the claim code is proved. The third is the boot's log line.
    A fourth reader is how the posture would reach a body anyone can fetch -
    /api/auth/config and /api/auth/needs-setup live in this same module."""
    readers = {fn.name for fn in _router_tree().body
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(isinstance(node, ast.Name) and node.id == "REQUIRE_MFA"
                       and isinstance(node.ctx, ast.Load) for node in ast.walk(fn))}
    assert readers == {"login", "setup_admin", "log_require_mfa"}


# -- the boot says which way it was read --------------------------------------

@pytest.fixture
def router_lines(monkeypatch):
    """Every line the auth router logs from here on, as (event, fields)."""
    lines = []
    monkeypatch.setattr(auth_router, "log",
                        lambda event, **fields: lines.append((event, fields)))
    return lines


def test_each_boot_logs_the_switch_on_or_off(monkeypatch, router_lines):
    """FIX. One line per boot with the effective state, off included: a guard
    that is silent when healthy cannot be told apart from one that is not
    running, and an operator who sets the variable has a line to look for."""
    from app import main
    monkeypatch.setattr(auth_router, "REQUIRE_MFA", True)
    asyncio.run(main._require_mfa_on_startup())
    monkeypatch.setattr(auth_router, "REQUIRE_MFA", False)
    asyncio.run(main._require_mfa_on_startup())
    assert router_lines == [("require_mfa", {"state": "on"}),
                            ("require_mfa", {"state": "off"})]


def test_the_boot_line_is_a_registered_startup_hook():
    """FIX. The def is not the guard, the registration is: a hook nothing
    registered would be a line no boot ever logs."""
    from app import main
    assert main._require_mfa_on_startup in main.app.router.on_startup


def test_a_boot_line_that_cannot_be_written_does_not_stop_the_boot(monkeypatch):
    """FIX. The switch is read at import and enforced in the routes; the hook
    only says its state, so its failure is logged and the boot goes on."""
    from app import main
    said = []
    monkeypatch.setattr(main, "log_error", lambda event, **fields: said.append((event, fields)))

    def _broken():
        raise OSError("log volume is full")
    monkeypatch.setattr(auth_router, "log_require_mfa", _broken)
    asyncio.run(main._require_mfa_on_startup())
    assert said == [("require_mfa_line_crashed", {"error": "OSError: log volume is full"})]


# -- the account lock comes first ---------------------------------------------

PW = "FlagAndLock1"
WRONG = "not-the-password"
REFUSED_401 = (401, {"detail": "Invalid username or password"})


@pytest.fixture
def account():
    """A throwaway password account with no authenticator, made straight in
    the store, left unlocked and counted at zero afterwards."""
    name = f"mfaflag_{uuid.uuid4().hex[:8]}"
    uid = create_user(name, hash_password(PW))
    reset_failed_attempts(uid)
    try:
        yield name, uid
    finally:
        unlock_user(uid)
        reset_failed_attempts(uid)


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    return r.status_code, r.json()


def _bookkeeping(uid):
    row = get_user_by_id(uid)
    return row.get("failed_attempts"), row.get("locked_until")


def test_a_locked_account_gets_the_locks_answer_not_the_flags(client, account, monkeypatch):
    """CONTROL. A correct password on a locked account with no authenticator:
    the lock's 401, with the flag off and with it on. If the flag's refusal
    ran first, the 403 would tell a caller who holds the password that the
    lock had not refused them - and would say this deployment requires
    two-factor to someone the lock is there to keep out."""
    name, uid = account
    lock_user(uid, (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat())
    before = _bookkeeping(uid)
    for flag in (False, True):
        monkeypatch.setattr(auth_router, "REQUIRE_MFA", flag)
        assert _login(client, name, PW) == REFUSED_401, f"flag={flag}"
        assert _login(client, name, WRONG) == REFUSED_401, f"flag={flag}"
    assert _bookkeeping(uid) == before          # the locked path writes nothing


def test_the_flags_refusal_moves_no_lockout_bookkeeping(client, account, monkeypatch):
    """CONTROL. The 403 comes after a CORRECT password, so it is not a failed
    attempt and must not count toward the lock - a person told to go and
    enroll would lock themselves out by retrying. Nor is it a sign-in, so it
    must not clear the count either: only a session that was really granted
    does."""
    name, uid = account
    assert _login(client, name, WRONG) == REFUSED_401
    assert _login(client, name, WRONG) == REFUSED_401
    assert _bookkeeping(uid) == (2, None)

    monkeypatch.setattr(auth_router, "REQUIRE_MFA", True)
    for _ in range(4):                          # two failures + four refusals > the lock's five
        status, body = _login(client, name, PW)
        assert status == 403, body
        assert "MFA is required" in body["detail"]
        assert _bookkeeping(uid) == (2, None)

    monkeypatch.setattr(auth_router, "REQUIRE_MFA", False)
    status, body = _login(client, name, PW)
    assert status == 200 and body["access_token"], body
    assert _bookkeeping(uid) == (0, None)       # a granted session is what clears it


# -- the documents say what the code does -------------------------------------

def _one_line(path: Path) -> str:
    """A file as one line with its comment marks dropped, so a sentence
    wrapped across lines still reads. Skips where the file is not beside this
    suite: the image ships backend/ alone, without the repo's documents."""
    if not path.exists():
        pytest.skip(f"{path.name} is not beside this suite")
    return " ".join(path.read_text(encoding="utf-8").replace("#", " ").split())


@pytest.mark.parametrize("name", (".env.example", "knowledge/onboarding.md",
                                  "docs/runbook.md"))
def test_each_document_says_another_value_stops_the_backend(name, router_lines):
    """FIX. And each names the line the boot really logs: the event comes from
    the code here, so renaming it without the documents fails."""
    auth_router.log_require_mfa()
    (event, fields), = router_lines
    text = _one_line(REPO / name)
    assert "stops the backend at start" in text
    assert event in text and set(fields) == {"state"} and "state" in text


@pytest.mark.parametrize("path", (REPO / "knowledge" / "onboarding.md", APP / HOME,
                                  BACKEND / "tests" / "test_setup_claim_code.py"),
                         ids=lambda p: p.name)
def test_nothing_says_a_claim_under_the_flag_is_past_recovery(path):
    """FIX. Three places said a deployment claimed under the flag left an Owner
    who could never sign in, with no way back short of editing the database.
    Turning the flag off and restarting is the way back, and the claim's own
    refusal says so."""
    text = _one_line(path)
    assert "short of editing the database" not in text
    assert "can never sign in" not in text
