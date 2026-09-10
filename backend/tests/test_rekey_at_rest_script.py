"""The offline re-key sweep, run the way an operator runs it: a subprocess of
this interpreter against a throwaway SQLite file. This is the proof that the
remedy the T9 refusal names IMPORTS AND RUNS on this surface - a 2026-09-06
design named a script that could not import where its 403 pointed.
"""
import base64
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rekey_at_rest.py"
OLD = "old-secret-" + "a" * 20
NEW = "new-secret-" + "b" * 20
OTHER = "other-secret-" + "c" * 20
SEED = b"JBSWY3DPEHPK3PXP"


def _f(secret):
    # The app derives its Fernet key the same way (crypto_at_rest module) - a
    # SHA-256 digest of the configured value, base64 urlsafe.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _db(tmp_path, rows_users, rows_config):
    p = tmp_path / "history.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, mfa_secret TEXT)")
    c.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    c.executemany("INSERT INTO users VALUES (?, ?)", rows_users)
    c.executemany("INSERT INTO config VALUES (?, ?)", rows_config)
    c.commit()
    c.close()
    return p


def _run(db, *args, old=OLD, new=NEW):
    env = {**os.environ, "REKEY_OLD_SECRET": old, "REKEY_NEW_SECRET": new}
    return subprocess.run([sys.executable, str(SCRIPT), "--db", str(db), *args],
                          capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL)


def _rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_rekeys_every_tenant_and_leaves_the_rest_alone(tmp_path):
    db = _db(tmp_path,
             [(1, _f(OLD).encrypt(SEED).decode()), (2, "LEGACYPLAINTEXT23"), (3, None)],
             [("openai_api_key", _f(OLD).encrypt(b"sk-test").decode()),
              ("system_prompt", "You are helpful.")])
    r = _run(db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rekeyed 2" in r.stdout and "undecryptable under both keys 0" in r.stdout
    (u1,), (u2,), (u3,) = _rows(db, "SELECT mfa_secret FROM users ORDER BY id")
    assert _f(NEW).decrypt(u1.encode()) == SEED
    with pytest.raises(InvalidToken):
        _f(OLD).decrypt(u1.encode())               # no longer readable under OLD
    assert u2 == "LEGACYPLAINTEXT23" and u3 is None
    cfg = dict(_rows(db, "SELECT key, value FROM config"))
    assert _f(NEW).decrypt(cfg["openai_api_key"].encode()) == b"sk-test"
    assert cfg["system_prompt"] == "You are helpful."
    # Idempotent: a second run finds everything already under NEW.
    r = _run(db)
    assert r.returncode == 0 and "rekeyed 0" in r.stdout, r.stdout


def test_a_row_under_neither_key_is_reported_and_never_rewritten(tmp_path):
    stuck = _f(OTHER).encrypt(SEED).decode()
    db = _db(tmp_path, [(1, _f(OLD).encrypt(SEED).decode()), (4, stuck)], [])
    r = _run(db)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "STUCK: users.mfa_secret where id=4" in r.stdout
    assert _rows(db, "SELECT mfa_secret FROM users WHERE id=4")[0][0] == stuck
    good = _rows(db, "SELECT mfa_secret FROM users WHERE id=1")[0][0]
    assert _f(NEW).decrypt(good.encode()) == SEED   # the good row still moved


def test_dry_run_writes_nothing(tmp_path):
    db = _db(tmp_path, [(1, _f(OLD).encrypt(SEED).decode())],
             [("anthropic_api_key", _f(OLD).encrypt(b"k").decode())])
    before = db.read_bytes()
    r = _run(db, "--dry-run")
    assert r.returncode == 0 and r.stdout.startswith("DRY RUN"), r.stdout + r.stderr
    assert "rekeyed 2" in r.stdout
    assert db.read_bytes() == before


def test_secrets_never_ride_argv(tmp_path):
    db = _db(tmp_path, [], [])
    r = _run(db, "--old", OLD, "--new", NEW)
    assert r.returncode == 3 and "unrecognized" in r.stderr   # usage is 3; 2 means stuck rows
    r = _run(db, old="", new="")                      # no terminal, no env: refuse
    assert r.returncode == 3 and "never on the command line" in r.stderr


def test_only_this_surfaces_secrets_are_accepted(tmp_path):
    db = _db(tmp_path, [], [])
    r = _run(db, "--secret", "SECRET_KEY")
    assert r.returncode != 0, r.stdout + r.stderr


def test_a_file_that_is_not_sqlite_exits_3_not_2(tmp_path):
    """Exit 2 is reserved for rows stuck under both keys; a file that is not a
    database is a usage error and must not read as stuck rows to a restore
    procedure that gates on 2 (2026-09-10 review)."""
    p = tmp_path / "history.db"
    p.write_bytes(b"not a database at all, just bytes to make sqlite refuse")
    r = _run(p)
    assert r.returncode == 3, r.stdout + r.stderr
