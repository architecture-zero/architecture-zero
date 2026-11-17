"""The restore flag (2026-11-17): a database put back from an archive keeps
no session.

A restore replaces the database whole, so every refresh token revoked after
the archive was taken - a sign-out, a password change, a deactivation - came
back live until its own expiry, and no boot could tell. FIX: a restore writes
restored.json into the data directory; the next boot revokes every session in
the database, records it, logs it and deletes the flag (app/restore_flag.py).

The two boot tests are real boots: `import app.main` in an interpreter of its
own, on an archive copy of this suite's database, with the flag in that
copy's data directory - so they prove the main.py call as well as the
reader. The rest call the reader in this process, on this suite's database,
which put_back returns to its state before each test.

Labels: FIX fails on the code before this change (the reader did not exist,
nothing read the flag); CONTROL is the same setup without the flag, so a FIX
cannot pass by accident; GUARD held before and must keep holding.
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.db import DATABASE_URL, engine, get_session
from app.models import RefreshToken, User

BACKEND = Path(__file__).resolve().parents[1]
FLAG = "restored.json"
FUTURE = "2099-01-01T00:00:00+00:00"


def _db_file() -> str:
    return make_url(DATABASE_URL).database


def _copy_database(src: str, dst: str) -> None:
    engine.dispose()
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    engine.dispose()


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def put_back(tmp_path):
    """These tests revoke every session in the suite's one database; this
    puts it back as it was when each one ends."""
    before = str(tmp_path / "before-this-test.db")
    _copy_database(_db_file(), before)
    yield
    _copy_database(before, _db_file())


@pytest.fixture
def owner_id(client):
    with get_session() as db:
        return db.query(User.id).order_by(User.id).first()[0]


def _live() -> int:
    with get_session() as db:
        return db.query(RefreshToken).filter(RefreshToken.revoked == False).count()  # noqa: E712


def _add_sessions(user_id: int, *names: str) -> None:
    with get_session() as db:
        for n in names:
            db.add(RefreshToken(user_id=user_id, token_hash=_hash(n),
                                expires_at=FUTURE, revoked=False))


def _flag(directory: Path, body: str = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FLAG
    path.write_text(body if body is not None else json.dumps(
        {"restored_at": "2026-11-17T09:30:00Z", "archive": "az_backup_20261117.tar.gz"}),
        encoding="utf-8")
    return path


@pytest.fixture
def lines(monkeypatch):
    """Every line the reader logs, INFO and ERROR, in order."""
    from app import restore_flag
    seen = []
    monkeypatch.setattr(restore_flag, "log", lambda e, **kw: seen.append(("info", e, kw)))
    monkeypatch.setattr(restore_flag, "log_error", lambda e, **kw: seen.append(("error", e, kw)))
    return seen


# -- a real boot, on a restored copy -----------------------------------------

def _restored_instance(tmp_path: Path, owner: int) -> Path:
    """An instance directory holding an archive of this suite's database in
    which two sessions are live - what a restore puts back."""
    inst = tmp_path / "instance"
    (inst / "data").mkdir(parents=True)
    db = inst / "data" / "history.db"
    _copy_database(_db_file(), str(db))
    con = sqlite3.connect(db)
    try:
        with con:
            # Every session table the copy has (the MCP door's OAuth tokens
            # where the surface issues them), so exactly two are live.
            for t in ("refresh_tokens", "oauth_tokens"):
                if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                               "AND name = ?", (t,)).fetchone():
                    con.execute(f"UPDATE {t} SET revoked = 1")
            for n in ("token_9f8e7d6c5b4a3210", "token_0123456789abcdef"):
                con.execute("INSERT INTO refresh_tokens (user_id, token_hash, expires_at, revoked) "
                            "VALUES (?, ?, ?, 0)", (owner, _hash(n), FUTURE))
    finally:
        con.close()
    return inst


def _boot(inst: Path) -> subprocess.CompletedProcess:
    data = (inst / "data").as_posix()
    env = {**os.environ,
           "DATABASE_URL": f"sqlite:///{data}/history.db",
           "HISTORY_DB_PATH": f"{data}/history.db",
           "CHROMA_PATH": data, "DATA_DIR": data, "BACKUP_STATUS_DIR": data,
           "BOOT_HISTORY_DIR": data, "HELP_SYNC_STATE_DIR": data,
           "EVIDENCE_STATUS_DIR": data, "LOG_DIR": (inst / "logs").as_posix()}
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=BACKEND, env=env,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
    assert r.returncode == 0, f"the boot failed (exit {r.returncode})\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
    return r


def _live_in(inst: Path) -> int:
    con = sqlite3.connect(inst / "data" / "history.db")
    try:
        return con.execute("SELECT count(*) FROM refresh_tokens WHERE revoked = 0").fetchone()[0]
    finally:
        con.close()


def test_a_boot_after_a_restore_revokes_every_session_and_deletes_the_flag(tmp_path, owner_id):
    """FIX. The archive carries two live sessions - in the database it
    replaced, they had been ended. The boot ends them again, logs it with the
    count and the archive's name, and deletes the flag."""
    inst = _restored_instance(tmp_path, owner_id)
    flag = _flag(inst / "data")
    assert _live_in(inst) == 2

    r = _boot(inst)

    assert _live_in(inst) == 0, "every session in a restored database is revoked"
    assert not flag.exists(), "the flag fires once"
    out = r.stdout + r.stderr
    assert '"event": "sessions_revoked_after_restore"' in out, out[-3000:]
    assert '"tokens_revoked": 2' in out and "az_backup_20261117.tar.gz" in out, out[-3000:]


def test_a_boot_with_no_flag_keeps_every_session(tmp_path, owner_id):
    """GUARD (and the boot test's CONTROL). The same restored copy with no
    flag: a plain boot signs nobody out."""
    inst = _restored_instance(tmp_path, owner_id)
    r = _boot(inst)
    assert _live_in(inst) == 2
    assert "sessions_revoked_after_restore" not in r.stdout + r.stderr


# -- the reader, in this process ---------------------------------------------

def test_no_flag_changes_nothing(put_back, tmp_path, owner_id, lines):
    """CONTROL. No flag: nothing read, nothing revoked, nothing logged."""
    from app.restore_flag import consume_restore_flag
    _add_sessions(owner_id, "token_9f8e7d6c5b4a3210")
    live = _live()
    assert consume_restore_flag(str(tmp_path)) is None
    assert _live() == live and lines == []


def test_the_flag_revokes_every_live_session_once(put_back, tmp_path, owner_id, lines):
    """FIX. Every live refresh token is revoked in one pass, the log line
    carries the count and the flag's fields, and the flag is gone - so the
    next boot, with sessions opened since, revokes nothing."""
    from app.restore_flag import consume_restore_flag
    _add_sessions(owner_id, "token_9f8e7d6c5b4a3210", "token_0123456789abcdef")
    live = _live()
    flag = _flag(tmp_path)

    got = consume_restore_flag(str(tmp_path))

    assert _live() == 0 and not flag.exists()
    assert got["revoked"] is True and got["flag_removed"]
    assert got["tables"]["refresh_tokens"] == live
    assert got["tokens_revoked"] == sum(got["tables"].values())
    info = [kw for lvl, e, kw in lines if e == "sessions_revoked_after_restore"]
    assert len(info) == 1 and info[0]["tokens_revoked"] == got["tokens_revoked"], lines
    assert info[0]["archive"] == "az_backup_20261117.tar.gz" and info[0]["flag_readable"] is True

    _add_sessions(owner_id, "token_fedcba9876543210")
    assert consume_restore_flag(str(tmp_path)) is None
    assert _live() == 1, "a session opened after the restore boot is kept"


@pytest.mark.parametrize("body", ["not json {", "[1, 2]", "x" * 5000, ""],
                         ids=["not-json", "not-an-object", "oversize", "empty"])
def test_an_unreadable_flag_still_revokes_and_is_deleted(put_back, tmp_path, owner_id, lines, body):
    """FIX. A flag that is not a small JSON object fails toward one more
    sign-in: every session is revoked all the same, the flag is deleted, and
    the boot says the flag was unreadable at ERROR."""
    from app.restore_flag import consume_restore_flag
    _add_sessions(owner_id, "token_9f8e7d6c5b4a3210")
    flag = _flag(tmp_path, body)

    got = consume_restore_flag(str(tmp_path))

    assert _live() == 0 and not flag.exists()
    assert got["flag_readable"] is False
    assert [e for lvl, e, _ in lines if lvl == "error"] == ["restore_flag_unreadable"], lines


def test_a_failed_revoke_keeps_the_flag_for_the_next_boot(put_back, tmp_path, owner_id, lines, monkeypatch):
    """FIX. When the revoke cannot run, the flag stays and the boot says so at
    ERROR - nothing is deleted on a promise. The next boot, with the database
    back, revokes and deletes it."""
    import app.db
    from app.restore_flag import consume_restore_flag
    _add_sessions(owner_id, "token_9f8e7d6c5b4a3210")
    live = _live()
    flag = _flag(tmp_path)

    class _Down:
        def begin(self):
            raise RuntimeError("database is locked")
    real = app.db.engine
    monkeypatch.setattr(app.db, "engine", _Down())

    got = consume_restore_flag(str(tmp_path))

    assert got == {"revoked": False, "flag_kept": True}
    assert flag.exists() and _live() == live
    assert [e for lvl, e, _ in lines if lvl == "error"] == ["sessions_revoke_after_restore_failed"]

    monkeypatch.setattr(app.db, "engine", real)
    assert consume_restore_flag(str(tmp_path))["revoked"] is True
    assert _live() == 0 and not flag.exists()


def test_a_flag_that_cannot_be_deleted_is_named_at_error(put_back, tmp_path, owner_id, lines, monkeypatch):
    """FIX. The revoke stands when the flag cannot be removed, and the ERROR
    line says every boot will revoke again until someone removes it."""
    from app import restore_flag
    _add_sessions(owner_id, "token_9f8e7d6c5b4a3210")
    _flag(tmp_path)

    real_remove = os.remove

    def _refuse(path):
        if os.path.basename(path) == FLAG:
            raise PermissionError("read-only file system")
        return real_remove(path)
    monkeypatch.setattr(restore_flag.os, "remove", _refuse)

    got = restore_flag.consume_restore_flag(str(tmp_path))

    assert got["revoked"] is True and got["flag_removed"] is False and _live() == 0
    errors = [(e, kw) for lvl, e, kw in lines if lvl == "error"]
    assert [e for e, _ in errors] == ["restore_flag_not_removed"]
    assert "every boot" in errors[0][1]["note"] and FLAG in errors[0][1]["note"]


def test_a_directory_in_the_flags_place_revokes_and_is_named_at_error(put_back, tmp_path, owner_id, lines):
    """FIX. Only a regular file at the flag's name is opened (a pipe there
    would hold the boot on the open). A directory there still revokes - it
    is not a flag this platform wrote, and the safe side is one more
    sign-in - and, since it cannot be removed as a file, the ERROR line says
    every boot will revoke again until someone does."""
    from app.restore_flag import consume_restore_flag
    _add_sessions(owner_id, "token_9f8e7d6c5b4a3210")
    (tmp_path / FLAG).mkdir()

    got = consume_restore_flag(str(tmp_path))

    assert _live() == 0 and got["flag_readable"] is False and got["flag_removed"] is False
    assert [e for lvl, e, _ in lines if lvl == "error"] == ["restore_flag_unreadable",
                                                           "restore_flag_not_removed"]


def test_the_reader_never_raises(tmp_path, monkeypatch, lines):
    """FIX. A boot never dies here: anything the reader did not expect is one
    ERROR line, and the flag is left for the next boot."""
    from app import restore_flag
    _flag(tmp_path)

    def _boom(data_dir=None):
        raise OSError("disk gone")
    monkeypatch.setattr(restore_flag, "flag_path", _boom)
    assert restore_flag.consume_restore_flag(str(tmp_path)) is None
    assert [e for lvl, e, _ in lines] == ["restore_flag_crashed"]
    assert (tmp_path / FLAG).exists()


def test_the_flag_is_read_where_force_rebuild_is_read():
    """FIX. One data directory for both one-shot levers, so a runbook that
    names backend/data/ for one names the right place for the other."""
    from app import chroma_maintenance
    from app.database import _CHROMA_PATH
    from app.restore_flag import RESTORE_FLAG_FILE, flag_path
    assert RESTORE_FLAG_FILE == FLAG
    assert os.path.dirname(flag_path()) == os.path.abspath(_CHROMA_PATH)
    assert chroma_maintenance._FORCE_REBUILD_FILE == "force-rebuild.json"


def test_main_reads_the_flag_right_after_the_schema_step():
    """FIX. The call sits between the schema step and everything after it:
    the tables exist, and nothing has been served or swept yet."""
    src = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    schema = src.index("\n_create_schema()")
    call = src.index("\n_consume_restore_flag()")
    config = src.index("\ninit_config_db()")
    assert schema < call < config
