"""A start that is going to be refused is refused before anything is written.

Every on/off setting is read by one rule (app/env_flags.py; the rule itself
is pinned in test_env_flags_strict.py), and a value that is neither of a
setting's two words refuses to start. This file is about WHEN.

THE GAP. Each module read its own setting when it was imported, so a refusal
came from wherever that module sat in the import order: after the database
module, the vector store's client and the logger had been imported, and for
a late one after the tables were made and the config seeded. A setting read
only when it is used - not at import - did not refuse a start at all: the
backend came up and the refusal waited for the first call.

NOW the backend's first act is check_all(): every row of the table, whether
or not any module has asked for it yet, before app/main.py imports anything
else. A refused start has imported nothing, opened nothing and written
nothing. `python -m app.env_flags` is the same check by itself, for an
entrypoint that does work before the app is imported.

Each test says whether it is a FIX (it fails on the commit before this file)
or a CONTROL (it holds on both).
"""
import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import env_flags

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
REPO = BACKEND.parent
NAMES = list(env_flags.names())
# A value nobody would type, to prove a refusal does not quote what it read.
MARKER = "Zq7-pasted-by-mistake"

# -- this deployment's facts ---------------------------------------------------
#
# Rows tried one at a time against a whole start: the first row of the table,
# one read by a module that is imported early, one read by a module imported
# late, the two-factor switch (whose own refusal used to be the earliest),
# and the one row no module reads at import (the boot sync's switch is read
# by the startup hook).
REFUSED_FIRST = ["ENABLE_AUTH", "ENABLE_INJECTION_PROTECTION", "SELF_CHECK_BACKUP",
                 "REQUIRE_MFA", "BOOT_BACKGROUND_SYNC"]
# The script the image runs, where it runs one before the server; None where
# the image's command is the server itself.
ENTRYPOINT = None
# Settings that are not on/off whose refusal used to quote the value.
QUOTED_A_VALUE = ["MAX_CONTEXT_TOKENS"]
# Where an operator reads what a start that did not come up looks like.
RUNBOOKS = ("docs/runbook.md",)


# -- the check is the first thing main does -----------------------------------

def test_main_checks_every_setting_before_it_imports_anything_else():
    """FIX. The first two statements after the module's docstring: the import
    of the check and its call. An import placed above them is a module that
    can open or write something on a start that is about to be refused."""
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    assert isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant)
    assert ast.unparse(tree.body[1]) == (
        "from app.env_flags import check_all as _check_on_off_settings")
    assert ast.unparse(tree.body[2]) == "_check_on_off_settings()"


# -- every row, not only the ones a module happened to import ------------------

@pytest.mark.parametrize("name", NAMES)
def test_the_check_reads_every_row(monkeypatch, name):
    """FIX. One row at a time set to a value that is neither word, every
    other row left as the suite has it: the check refuses, and names that
    row."""
    env_flags.check_all()                       # the suite's own values all read
    monkeypatch.setenv(name, MARKER)
    with pytest.raises(RuntimeError) as refusal:
        env_flags.check_all()
    assert str(refusal.value).startswith(f"SECURITY: {name} ")
    assert MARKER.lower() not in str(refusal.value).lower()


def test_the_check_names_the_first_bad_row_in_the_tables_order(monkeypatch):
    """FIX. Two bad values, one refusal: the earlier row. An operator fixes
    one line per start, in an order that does not move between starts."""
    first, last = NAMES[0], NAMES[-1]
    monkeypatch.setenv(last, "yes")
    monkeypatch.setenv(first, "1")
    with pytest.raises(RuntimeError) as refusal:
        env_flags.check_all()
    assert str(refusal.value).startswith(f"SECURITY: {first} ")


# -- the same check, by itself -------------------------------------------------

def _host_env(tmp_path, values: dict) -> dict:
    """The suite's environment with every on/off setting unset but `values`,
    and every path setting pointed below a directory that is empty and stays
    the only place a start may write."""
    host = tmp_path / "host"
    host.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in NAMES and k != "PROVIDER"}
    for var in ("DATA_DIR", "BACKUP_STATUS_DIR", "BOOT_HISTORY_DIR", "HELP_SYNC_STATE_DIR",
                "EVIDENCE_STATUS_DIR", "CHROMA_PATH", "LOG_DIR", "FASTEMBED_CACHE", "KB_DIR"):
        env[var] = str(host / var.lower())
    env["DATABASE_URL"] = f"sqlite:///{(host / 'the.db').as_posix()}"
    env["HISTORY_DB_PATH"] = str(host / "history.db")
    env["JWT_SECRET_KEY"] = "a-secret-for-the-fresh-start-probe-only"
    env["SELF_CHECK_INTERVAL_SECONDS"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(values)
    return env


def _left_behind(tmp_path) -> list[str]:
    host = tmp_path / "host"
    return sorted(p.relative_to(host).as_posix() for p in host.rglob("*"))


@pytest.mark.parametrize("name", NAMES)
def test_the_check_by_itself_exits_1_with_one_line(tmp_path, name):
    """FIX. `python -m app.env_flags`: exit 1, the refusal as the only line
    on the error stream, nothing on the output stream, and the value nowhere.
    It opens nothing, so the directory a start would write into stays empty."""
    done = subprocess.run([sys.executable, "-m", "app.env_flags"], capture_output=True,
                          text=True, env=_host_env(tmp_path, {name: MARKER}),
                          cwd=str(BACKEND), timeout=120)
    assert done.returncode == 1, done.stderr
    assert done.stdout == ""
    line, = done.stderr.strip().splitlines()
    assert line.startswith(f"SECURITY: {name} ")
    assert f"{name}=true or {name}=false" in line
    assert MARKER.lower() not in (done.stdout + done.stderr).lower()
    assert _left_behind(tmp_path) == []


@pytest.mark.parametrize("values", [{}, {"ENABLE_AUTH": "TRUE", "ENABLE_AUDIT_LOG": "false",
                                         "ENABLE_RATE_LIMIT": ""}],
                         ids=["nothing_set", "words_and_an_empty_line"])
def test_the_check_by_itself_exits_0_and_says_nothing(tmp_path, values):
    """CONTROL against the commit before (the module ran and said nothing
    there too; before the table there was no module to run). Every row
    recognised: exit 0 and not a word - no value, no state, nothing for a log
    to carry."""
    done = subprocess.run([sys.executable, "-m", "app.env_flags"], capture_output=True,
                          text=True, env=_host_env(tmp_path, values),
                          cwd=str(BACKEND), timeout=120)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert _left_behind(tmp_path) == []


# -- a refused start has imported nothing and written nothing ------------------
#
# The whole app, as a start imports it, in a fresh interpreter. The child
# records what a start does to the world as it happens - every file opened
# for writing, every directory made, every database and socket connection -
# through the interpreter's own audit events, so a write is seen wherever it
# lands and not only where this test thought to look. It then reports which
# of the app's modules were imported, and the test reads what was left in
# the one directory every path setting points into. The vector store is the
# REAL one on the refused start: its client is opened by an import, and
# opening it makes its directory and its database file, so a check that ran
# after that import would be seen here. Only the start that is not refused
# (the control) gets a stand-in, as conftest stands one in for the client.

_START = "\n".join((
    "import json, os, sqlite3, sys",
    "events = []",
    "WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC",
    "def hook(name, args):",
    "    try:",
    "        if name == 'open':",
    "            path, mode, flags = args",
    "            wrote = ((isinstance(mode, str) and set(mode) & set('wax+'))",
    "                     or (isinstance(flags, int) and flags & WRITE))",
    "            if wrote and '__pycache__' not in str(path):",
    "                events.append('open-for-write')",
    "        elif name in ('os.mkdir', 'os.rename', 'os.remove', 'os.rmdir',",
    "                      'os.truncate', 'sqlite3.connect', 'socket.connect'):",
    "            events.append(name)",
    "    except Exception:",
    "        pass",
    "sys.addaudithook(hook)",
    "if os.environ.get('VECTOR_STORE_STAND_IN'):",
    "    from unittest.mock import MagicMock",
    "    sys.modules['chromadb'] = MagicMock(name='chromadb')",
    "said = {'refusal': ''}",
    "try:",
    "    import app.main",
    "    said['result'] = 'STARTED'",
    "except RuntimeError as refusal:",
    "    said['result'] = 'REFUSED'",
    "    said['refusal'] = str(refusal)",
    "said['app_modules'] = sorted(m for m in sys.modules if m.startswith('app.')",
    "                             and m not in ('app.main', 'app.env_flags'))",
    "said['events'] = sorted(set(events))",
    "path = os.environ['DATABASE_URL'].replace('sqlite:///', '')",
    "said['tables'] = 0",
    "if os.path.exists(path) and os.path.getsize(path):",
    "    said['tables'] = sqlite3.connect(path).execute(",
    "        \"select count(*) from sqlite_master where type = 'table'\").fetchone()[0]",
    "print('START=' + json.dumps(said))",
    "sys.stdout.flush()",
    "os._exit(0)",          # what was imported is of no interest: skip its teardown
))


def _start(tmp_path, values: dict, stand_in: bool = False):
    env = _host_env(tmp_path, values)
    if stand_in:
        env["VECTOR_STORE_STAND_IN"] = "yes"
    done = subprocess.run([sys.executable, "-c", _START], capture_output=True, text=True,
                          env=env, cwd=str(BACKEND), timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    line = [ln for ln in done.stdout.splitlines() if ln.startswith("START=")][-1]
    return json.loads(line[len("START="):]), done


@pytest.mark.parametrize("name", REFUSED_FIRST)
def test_a_refused_start_has_imported_nothing_and_written_nothing(tmp_path, name):
    """FIX. One setting with a value that is neither word. The start is
    refused with that setting's name; not one other module of the app was
    imported; no file was opened for writing, no directory made, no database
    or socket connected; the database file does not exist; and the directory
    every path setting points into is as empty as it began.

    On the commit before, the same value was refused only when the module
    that owns the setting was imported - after the database module, the
    logger and whatever else stood above it - and for a setting read only
    when it is used, the start was not refused at all. (Where a row's owner
    happens to be the first module main imports, its leg held there too: a
    control by position, which is what this file turns into a rule.)"""
    said, done = _start(tmp_path, {name: MARKER})
    assert said["result"] == "REFUSED", said
    assert said["refusal"].startswith(f"SECURITY: {name} ")
    assert said["app_modules"] == []
    assert said["events"] == []
    assert said["tables"] == 0
    assert _left_behind(tmp_path) == []
    assert MARKER.lower() not in (done.stdout + done.stderr).lower()


def test_a_start_that_is_not_refused_is_seen_writing(tmp_path):
    """CONTROL, and the control OF the test above: the same child with every
    setting recognised starts, imports the app, makes its tables and is seen
    doing it - so 'no event, no file' above is a start that wrote nothing,
    not an instrument that cannot see a write."""
    said, _ = _start(tmp_path, {"ENABLE_AUTH": "true", "ENABLE_AUDIT_LOG": "false"},
                     stand_in=True)
    assert said["result"] == "STARTED", said
    assert len(said["app_modules"]) > 10
    assert said["tables"] > 0
    assert "sqlite3.connect" in said["events"]
    assert _left_behind(tmp_path) != []


# -- what starts the backend ---------------------------------------------------

def test_what_the_image_runs_first_is_the_check():
    """FIX where the image runs a script, CONTROL where it runs the server.
    The image's command is either the server itself, whose first import is
    app/main.py - the check - or a script; a script that does anything
    before the server (a schema migration) runs the check by itself first,
    under `set -e`, so a refused start has migrated nothing."""
    dockerfile = BACKEND / "Dockerfile"
    if not dockerfile.exists():
        pytest.skip("the Dockerfile is not beside this suite")
    command = [ln for ln in dockerfile.read_text(encoding="utf-8").splitlines()
               if ln.startswith("CMD ")][-1]
    if ENTRYPOINT is None:
        assert command.startswith('CMD ["uvicorn", "app.main:app"'), command
        return
    assert command == f'CMD ["/app/{ENTRYPOINT}"]', command
    raw = (BACKEND / ENTRYPOINT).read_bytes()
    assert b"\r" not in raw                     # a CR after the shebang cannot exec
    steps = [ln.strip() for ln in raw.decode("utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    # the shebang is a comment to this reading; the first two steps are the rule
    assert steps[:2] == ["set -e", "python3 -m app.env_flags"], steps[:4]
    assert len(steps) > 2                       # and something does run after it


@pytest.mark.skipif(ENTRYPOINT is None, reason="this image runs the server itself")
def test_a_refused_check_stops_the_script_before_its_next_step(tmp_path):
    """FIX. The script run for real, with stand-ins on the path for the
    three programs it calls. The check's stand-in refuses: nothing after it
    runs - no migration step, no server. Recognised: every step runs, in the
    script's order, the check first."""
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh on this machine's path")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ran = tmp_path / "ran.txt"
    stub = ("#!/bin/sh\n"
            'echo "$(basename "$0") $*" >> "$RAN"\n'
            'if [ "$(basename "$0") $1 $2" = "python3 -m app.env_flags" ]; then exit "$CHECK_EXIT"; fi\n'
            "exit 0\n")
    for name in ("python3", "alembic", "uvicorn"):
        (bin_dir / name).write_bytes(stub.encode("utf-8"))
        os.chmod(bin_dir / name, 0o755)
    script = (BACKEND / ENTRYPOINT).as_posix()

    def run(check_exit):
        if ran.exists():
            ran.unlink()
        env = dict(os.environ, RAN=ran.as_posix(), CHECK_EXIT=str(check_exit),
                   PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        done = subprocess.run([sh, script], capture_output=True, text=True, env=env,
                              cwd=str(tmp_path), timeout=120)
        steps = ran.read_text(encoding="utf-8").splitlines() if ran.exists() else []
        return done.returncode, [s.split()[0] + " " + " ".join(s.split()[1:3]) for s in steps]

    code, steps = run(1)
    assert code == 1 and steps == ["python3 -m app.env_flags"], (code, steps)
    code, steps = run(0)
    assert code == 0, steps
    assert steps[0] == "python3 -m app.env_flags"
    assert [s.split()[0] for s in steps] == ["python3", "python3", "alembic", "uvicorn"], steps


# -- a refusal names the variable, never the value ----------------------------

@pytest.mark.parametrize("variable", QUOTED_A_VALUE)
def test_an_older_refusal_no_longer_quotes_the_value(tmp_path, variable):
    """FIX. A setting that is not on/off and already refused a value it could
    not use - and quoted that value in the refusal. A value on the wrong
    line of an env file can be a secret, so the refusal now names the
    variable and what it takes, and the value is nowhere in what the process
    printed: not in the message, not in the traceback's chain."""
    done = subprocess.run(
        [sys.executable, "-c",
         "import sys\nfrom unittest.mock import MagicMock\n"
         "sys.modules['chromadb'] = MagicMock(name='chromadb')\nimport app.main\n"],
        capture_output=True, text=True, env=_host_env(tmp_path, {variable: MARKER}),
        cwd=str(BACKEND), timeout=600)
    assert done.returncode != 0, done.stdout
    last = done.stderr.strip().splitlines()[-1]
    assert variable in last, done.stderr[-2000:]
    assert MARKER.lower() not in (done.stdout + done.stderr).lower()


# -- the documents say what the code does -------------------------------------

def _one_line(path: Path) -> str:
    """A file as one line with its comment marks dropped, so a sentence
    wrapped across lines still reads. Skips where the file is not beside this
    suite: the image ships backend/ alone, without the repo's documents."""
    if not path.exists():
        pytest.skip(f"{path.name} is not beside this suite")
    return " ".join(path.read_text(encoding="utf-8").replace("#", " ").split())


@pytest.mark.parametrize("name", RUNBOOKS)
def test_the_runbook_says_a_refused_start_wrote_nothing_and_reads_red(name):
    """FIX. Where an operator reads about a start that did not come up: the
    check runs before anything is written, the health wait after a deploy
    fails on it, and the line to look for in the log."""
    text = _one_line(REPO / name)
    assert "before anything is written" in text
    assert "wait_backend_healthy.sh" in text
    assert "is set to a value that is neither true nor false" in text
