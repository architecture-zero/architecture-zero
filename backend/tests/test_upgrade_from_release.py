"""Today's code, booted on a database a release created.

This template has no migration system. A deployment's schema is whatever
create_all() built on the day of its first boot, and every change since
reaches it through the boot path alone: create_all for a new table, the ADD
COLUMN list and the one-time rebuild in app/db.py, the two at-rest sweeps, the
orphan sweep. The suite's own database is created by TODAY's models, so until
this file none of that had run against an old database here - an operator's
upgrade was its first execution.

The fixture is the schema exactly as the release created it, dumped from a
running instance of that release (tests/fixtures/). The rows are what a first
week leaves behind, in the formats that release wrote them: an Owner whose
second factor's seed is stored as written, a provider key saved through the
settings page and stored as written, a member, a conversation, a held upload,
a session somebody left signed in - and one row the release could not have
written through its own API, a refresh token whose account is gone, so the
orphan sweep has something to find.

WHEN A RELEASE CHANGES THE SCHEMA: dump its schema beside the others (the
command is in each fixture's header) and add the tag to RELEASES. v0.1.1
changed no model and no line of app/db.py, so the schema v0.1.1 creates is
v0.1.0's and one fixture stands for both. What this file is for is that the
schema the last release creates is always in that list.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

import pyotp
import pytest

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RELEASES = ["v0.1.0"]

OWNER, OWNER_PASSWORD = "owner", "Owner-Pass-before-upgrade1!"
MEMBER, MEMBER_PASSWORD = "casey", "Member-Pass-before-upgrade1!"
SESSION = "first-week-conversation"
SAID = ["What is the visitor parking code?", "It is on page two of the handbook."]
HELD = "vendor-note.md"


def _plant(db: Path, release: str, people: dict) -> None:
    """A first week on `release`, written the way that release wrote it."""
    from app.jwt_auth import hash_password
    con = sqlite3.connect(db)
    con.executescript((FIXTURES / f"release-{release}-schema.sql").read_text(encoding="ascii"))
    users = [
        (1, OWNER, hash_password(OWNER_PASSWORD), "owner", "{}", "finance", 1,
         "2026-08-30T09:00:00.000000", people["totp_seed"], 1, 0, None),
        (2, MEMBER, hash_password(MEMBER_PASSWORD), "member", "{}", "general", 1,
         "2026-08-30T09:05:00.000000", None, 0, 0, None),
    ]
    con.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", users)
    con.executemany("INSERT INTO config VALUES (?,?)", [
        ("system_prompt", "You answer from the documents and cite them."),
        ("instance_name", "First Week Co."),
        ("default_rag_enabled", "true"),
        ("anthropic_api_key", people["provider_key"]),      # as written: in the clear
    ])
    con.execute("INSERT INTO chat_sessions VALUES (1,?,2,?,'general',?,?)",
                (SESSION, SAID[0], "2026-08-31T10:00:00.000000", "2026-08-31T10:00:00.000000"))
    con.executemany(
        "INSERT INTO messages (session, user_id, role, content, model, timestamp) "
        "VALUES (?,2,?,?,'a-model',?)",
        [(SESSION, "user", SAID[0], "2026-08-31T10:00:00.000000"),
         (SESSION, "assistant", SAID[1], "2026-08-31T10:00:40.000000")])
    con.execute(
        "INSERT INTO audit_log (user_id, username, session_id, timestamp, prompt_hash, "
        "prompt_preview, response_length, model, use_rag, sources, duration_ms, ttft_ms, "
        "answer_lane) VALUES (2,?,?,?,?,?,35,'a-model',1,?,40000,38000,'model')",
        (MEMBER, SESSION, "2026-08-31T10:00:40.000000",
         hashlib.sha256(SAID[0].encode()).hexdigest(), SAID[0], json.dumps(["handbook.md"])))
    con.execute(
        "INSERT INTO quarantined_docs (source, department, trust_tier, text, findings, "
        "status, created_at) VALUES (?,'general','untrusted','held text',?,'held',?)",
        (HELD, json.dumps([{"type": "instruction_override", "severity": "high"}]),
         "2026-08-31T11:00:00.000000"))
    token_hash = hashlib.sha256(people["refresh_token"].encode()).hexdigest()
    con.executemany(
        "INSERT INTO refresh_tokens (user_id, token_hash, expires_at, revoked) VALUES (?,?,?,0)",
        [(2, token_hash, "2099-01-01T00:00:00+00:00"),
         # Its account is gone. The release deactivates accounts and never
         # deletes one, so its API cannot leave this row; a hand edit can.
         (99, "0" * 64, "2099-01-01T00:00:00+00:00")])
    con.commit()
    con.close()


def _boot(db: Path, chroma: Path, people: dict) -> tuple[dict, str]:
    """One boot of today's code on `db`, in an interpreter of its own, with a
    deployment's defaults rather than this suite's."""
    env = {**os.environ,
           "DATABASE_URL": f"sqlite:///{db.as_posix()}",
           "JWT_SECRET_KEY": "the-secret-this-deployment-has-always-had",
           "CHROMA_PATH": str(chroma),
           "ENABLE_AUTH": "true",
           "ENABLE_AUDIT_LOG": "true",
           "ALLOW_GUEST_MODE": "false",
           "UPGRADE_PEOPLE": json.dumps({
               **people, "owner": OWNER, "owner_password": OWNER_PASSWORD,
               "member": MEMBER, "member_password": MEMBER_PASSWORD,
               "session_id": SESSION})}
    r = subprocess.run([sys.executable, "-m", "tests.upgrade_boot"], cwd=BACKEND,
                       env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=300)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("REPORT ")]
    assert lines, (
        f"today's code did not boot on the old database (exit {r.returncode})\n"
        f"{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    # The report is the verdict, the way CI reads the junit file: chromadb's
    # native stack can take the interpreter down at teardown, AFTER the last
    # line was printed (CONTRIBUTING.md). Any other exit is a failure.
    assert r.returncode in (0, 134, 139, -6, -11), (
        f"exit {r.returncode} after the report\n{r.stderr[-3000:]}")
    return json.loads(lines[-1][len("REPORT "):]), r.stdout


def _rows(db: Path, sql: str) -> list:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


@pytest.fixture(scope="module", params=RELEASES)
def upgraded(request, tmp_path_factory):
    home = tmp_path_factory.mktemp(f"upgrade-{request.param}")
    people = {"totp_seed": pyotp.random_base32(),
              "provider_key": "provider-key-" + secrets.token_hex(8),
              "refresh_token": secrets.token_urlsafe(32)}
    db = home / "history.db"
    _plant(db, request.param, people)
    before = {t: _rows(db, f"SELECT COUNT(*) FROM {t}")[0][0]
              for (t,) in _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    report, out = _boot(db, home / "chroma", people)
    return {"db": db, "home": home, "people": people, "before": before,
            "report": report, "out": out}


def test_nothing_the_models_declare_is_missing_from_the_upgraded_database(upgraded):
    """The guard for every change after this one. A column added to a model
    reaches a NEW deployment through create_all and an upgraded one only if
    app/db.py carries it - and the suite's own database is always new, so a
    forgotten ALTER is green everywhere except on an operator's box."""
    r = upgraded["report"]
    assert r["missing_columns"] == {}, (
        "today's models declare columns an upgraded database does not have - "
        f"add them to _run_migrations in app/db.py: {r['missing_columns']}")
    assert r["missing_indexes"] == {}, (
        "today's models declare indexes an upgraded database does not have "
        f"(create_all never adds one to a table that exists): {r['missing_indexes']}")
    assert r["audit_write"] == "ok", r["audit_write"]


def test_every_row_the_release_wrote_is_still_there(upgraded):
    db, before = upgraded["db"], upgraded["before"]
    after = {t: _rows(db, f"SELECT COUNT(*) FROM {t}")[0][0] for t in before}
    # Three tables move: audit_log by exactly one (the helper's write,
    # asserted); refresh_tokens by an amount not checked here (the orphan is
    # swept, the helper's two sign-ins and one refresh add rows); config gains
    # the defaults today's code seeds.
    assert after.pop("audit_log") == before.pop("audit_log") + 1
    before.pop("refresh_tokens"), after.pop("refresh_tokens")
    # config only ever gains keys: the defaults today's code seeds.
    assert after.pop("config") >= before.pop("config")
    assert after == before
    assert _rows(db, "SELECT session, role, content FROM messages ORDER BY id") == [
        (SESSION, "user", SAID[0]), (SESSION, "assistant", SAID[1])]
    assert dict(_rows(db, "SELECT key, value FROM config"))["instance_name"] == "First Week Co."


def test_the_people_are_who_they_were_and_can_sign_in(upgraded):
    r = upgraded["report"]
    assert r["owner_password_step"] == {"status": 200, "mfa_required": True}
    assert r["owner_code_step"]["status"] == 200, (
        "the Owner's authenticator stopped working across the upgrade: "
        f"{r['owner_code_step']}")
    assert r["member_login"]["status"] == 200
    assert r["roster"]["users"] == [
        [MEMBER, "member", "general", False, False],
        [OWNER, "owner", "finance", True, False]]


def test_a_session_left_signed_in_carries_across(upgraded):
    assert upgraded["report"]["left_signed_in"]["status"] == 200, (
        upgraded["report"]["left_signed_in"])


def test_the_second_factor_seed_is_encrypted_by_the_boot_and_says_so(upgraded):
    (raw,) = _rows(upgraded["db"], "SELECT mfa_secret FROM users WHERE id = 1")[0]
    assert raw != upgraded["people"]["totp_seed"] and raw.startswith("gAAAAA"), (
        "the seed is still stored as written")
    assert "mfa seed sweep: encrypted 1 plaintext seed(s) at rest" in upgraded["out"]


def test_the_saved_provider_key_is_encrypted_by_the_boot_and_still_reads(upgraded):
    raw = dict(_rows(upgraded["db"], "SELECT key, value FROM config"))["anthropic_api_key"]
    assert raw != upgraded["people"]["provider_key"] and raw.startswith("gAAAAA")
    assert upgraded["report"]["provider_key_reads_as_saved"] is True
    assert upgraded["report"]["settings"] == {"status": 200, "anthropic_key_set": True}
    assert "provider-key sweep: encrypted 1 plaintext secret(s) at rest" in upgraded["out"]


def test_what_the_people_made_is_still_theirs(upgraded):
    r = upgraded["report"]
    assert r["history"] == {"status": 200, "contents": SAID}
    assert [SESSION, SAID[0]] in r["sidebar"]["sessions"]
    assert r["quarantine"] == {"status": 200, "held": [HELD]}


def test_the_orphan_is_swept_and_the_boot_says_what_it_removed(upgraded):
    db = upgraded["db"]
    assert _rows(db, "SELECT COUNT(*) FROM refresh_tokens WHERE user_id = 99") == [(0,)]
    assert _rows(db, "PRAGMA foreign_key_check") == []
    assert "fk orphan sweep: {'deleted': {'refresh_tokens': 1}" in upgraded["out"]


def test_a_second_boot_changes_nothing_more(upgraded):
    """Every step of the boot path claims to be idempotent. Read from the
    rows: the ciphertext a second boot finds is the ciphertext it leaves."""
    db = upgraded["db"]
    watched = ("SELECT id, mfa_secret FROM users ORDER BY id",
               "SELECT key, value FROM config WHERE key LIKE '%api_key'",
               "SELECT sql FROM sqlite_master ORDER BY name")
    first = [_rows(db, q) for q in watched]
    # The session the first boot refreshed was rotated; the second signs in
    # with the token that rotation left, which this file never saw - so the
    # second boot is handed a token that is no longer valid, and only its
    # sweeps are read.
    _, out = _boot(db, upgraded["home"] / "chroma", upgraded["people"])
    assert [_rows(db, q) for q in watched] == first
    assert "sweep: encrypted" not in out and "fk orphan sweep" not in out
