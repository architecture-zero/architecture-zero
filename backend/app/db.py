import os
from contextlib import contextmanager
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///{}".format(
    os.getenv("HISTORY_DB_PATH", "/app/data/history.db")
)

_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _sqlite else {},
    pool_pre_ping=True,
)

if _sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        # WAL: readers never block writers (rollback-journal mode let a slow
        # read - e.g. an endpoint doing HTTP work inside an open session -
        # starve every writer into "database is locked"). busy_timeout: brief
        # write contention waits instead of erroring at sqlite's 5s default.
        # foreign_keys: SQLite ships with FK enforcement OFF, so a ForeignKey
        # declared in models.py was decorative - a deleted parent silently
        # orphans its children, and sqlite id-reuse can then cross-wire an
        # orphan onto a brand-new row (observed on a downstream deployment,
        # 2026-09-04). Per-connection by design in SQLite, hence here.
        # sweep_fk_orphans() (called from init_db) cleans what the unenforced
        # past left behind; this governs every write from now on.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


@contextmanager
def get_session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _run_migrations():
    """Idempotent column additions for tables that predate schema changes."""
    # Instance forks append their own idempotent column additions here for
    # tables that predate a schema change. Each statement must be safe to run
    # against a database that already has the column - the except below is the
    # idempotency, so ALTER ... ADD COLUMN is the only shape that belongs here.
    stmts: list[str] = [
        # quarantined_docs.release_error - a release that fails now stays held
        # and records why, instead of reporting success it did not achieve.
        "ALTER TABLE quarantined_docs ADD COLUMN release_error TEXT",
        # audit_log.pii_out_* - the output-side PII receipt (2026-09-16): what
        # the answer lane's OutputFilter found in the finished answer. Rows
        # written before this, and every row while PII_OUTPUT_MODE is off,
        # read NULL - unknown, never 0.
        "ALTER TABLE audit_log ADD COLUMN pii_out_hits INTEGER",
        "ALTER TABLE audit_log ADD COLUMN pii_out_redacted INTEGER",
        "ALTER TABLE audit_log ADD COLUMN pii_out_types VARCHAR(120)",
    ]
    with engine.connect() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # column already exists


def _rebuild_chat_sessions_unique():
    """One-time rebuild of chat_sessions, for databases created before session
    ids became per-owner.

    The table shipped with a GLOBAL unique on session_id while the meta upsert
    looked rows up owner-scoped, so a second account's first message INSERTed
    into a key the first account held and raised. create_all() never alters an
    existing table and SQLite cannot drop an inline UNIQUE, so repairing an
    already-deployed database needs a rebuild - which is why this does not
    belong in _run_migrations() above, whose contract is ADD COLUMN only.

    Guarded and idempotent: it fires only when the 1-column unique auto-index is
    actually present, so a fresh database (already built correctly by
    create_all) and an already-repaired one both fall straight through. Logs on
    failure rather than swallowing - a schema repair that quietly does nothing
    is the thing being fixed here.
    """
    if not _sqlite:
        return
    with engine.connect() as conn:
        try:
            if not conn.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='chat_sessions'")).first():
                return
            stale = False
            for row in conn.execute(text("PRAGMA index_list('chat_sessions')")):
                name, unique, origin = row[1], row[2], row[3]
                if not unique or origin != "u":
                    continue
                cols = [r[2] for r in conn.execute(
                    text("PRAGMA index_info('%s')" % name))]
                if cols == ["session_id"]:
                    stale = True
                    break
            if not stale:
                return
            # No table carries a foreign key to chat_sessions, so the rename
            # needs no FK juggling. The old constraint is strictly stricter than
            # the new one, so the copy can never collide on existing data.
            conn.execute(text("""
                CREATE TABLE chat_sessions_rebuild (
                    id INTEGER NOT NULL,
                    session_id VARCHAR(255) NOT NULL,
                    user_id INTEGER,
                    name VARCHAR(300),
                    category VARCHAR(100) NOT NULL,
                    created_at VARCHAR(50) NOT NULL,
                    updated_at VARCHAR(50) NOT NULL,
                    PRIMARY KEY (id),
                    CONSTRAINT uq_chat_sessions_sid_user
                        UNIQUE (session_id, user_id))"""))
            conn.execute(text(
                "INSERT INTO chat_sessions_rebuild "
                "(id, session_id, user_id, name, category, created_at, updated_at) "
                "SELECT id, session_id, user_id, name, category, created_at, "
                "updated_at FROM chat_sessions"))
            conn.execute(text("DROP TABLE chat_sessions"))
            conn.execute(text(
                "ALTER TABLE chat_sessions_rebuild RENAME TO chat_sessions"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_chat_sessions_sid "
                              "ON chat_sessions (session_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user "
                              "ON chat_sessions (user_id)"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_sessions_sid_guest "
                "ON chat_sessions (session_id) WHERE user_id IS NULL"))
            conn.commit()
            print("chat_sessions: rebuilt with per-owner unique", flush=True)
        except Exception as exc:
            conn.rollback()
            print("chat_sessions unique rebuild FAILED: %s" % exc, flush=True)


def sweep_fk_orphans() -> dict:
    """One boot-time pass: remove/null child rows whose parent no longer exists.

    FK enforcement (the connect listener above) governs writes from NOW ON; it
    does nothing about rows an unenforced past already orphaned, and sqlite
    id-reuse can cross-wire such a row onto a brand-new parent. Fixpoint loop
    because deleting an orphan can orphan its own children; SET NULL
    declarations are honored by nulling instead of deleting. Runs on a DIRECT
    sqlite3 connection OUTSIDE the engine pool: per-connection pragma state
    rides a pooled connection back into the pool, so flipping FK off on one
    would disable enforcement for whichever request checks it out next (the
    fork suites caught exactly that on this sweep's first version) - and a
    fresh sqlite3 connection's own default is already the FK-OFF this sweep
    needs, which matters because under enforcement an orphan that is itself a
    parent cannot be deleted while its children exist. Cheap on a healthy
    file - PRAGMA
    foreign_key_check on a small DB is milliseconds - and returns counts so
    the boot log can say what moved.
    """
    if not _sqlite:
        return {"deleted": {}, "nulled": {}, "residual": 0}
    deleted: dict = {}
    nulled: dict = {}
    import sqlite3
    raw = sqlite3.connect(engine.url.database)
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA busy_timeout=15000")
        for _ in range(10):
            violations = cur.execute("PRAGMA foreign_key_check").fetchall()
            if not violations:
                break
            for table, rowid, _parent, fkid in violations:
                if rowid is None:  # WITHOUT ROWID table - none in this schema
                    continue
                on_delete, cols = "", []
                for r in cur.execute(
                        f'PRAGMA foreign_key_list("{table}")').fetchall():
                    if r[0] == fkid:
                        on_delete = (r[6] or "").upper()
                        cols.append(r[3])
                if on_delete == "SET NULL" and cols:
                    sets = ", ".join(f'"{c}" = NULL' for c in cols)
                    cur.execute(
                        f'UPDATE "{table}" SET {sets} WHERE rowid = ?',  # nosec B608 - identifiers from sqlite's own pragma output over THIS schema, value bound
                        (rowid,))
                    nulled[table] = nulled.get(table, 0) + 1
                else:
                    cur.execute(
                        f'DELETE FROM "{table}" WHERE rowid = ?',  # nosec B608 - same as above: schema-derived identifier, bound value
                        (rowid,))
                    deleted[table] = deleted.get(table, 0) + 1
        # Re-check AFTER the loop (2026-09-05 review): exhausting the pass
        # bound - or a chain of rowid-less violations the guard above skips -
        # must not be reported as a completed sweep. The claim comes from the
        # pragma, never from the loop having ended.
        residual = len(cur.execute("PRAGMA foreign_key_check").fetchall())
        raw.commit()
    finally:
        raw.close()
    return {"deleted": deleted, "nulled": nulled, "residual": residual}


def _encrypt_mfa_seeds_sweep() -> int:
    """One-time convergence: encrypt any plaintext MFA seeds at rest.

    Rows written before crypto_at_rest existed hold the raw TOTP seed; the
    read path tolerates them (prefix discriminator), so nothing breaks
    while they exist - this sweep just converges them so the tolerance
    window closes. Idempotent: ciphertext rows are skipped by the same
    prefix check the read path uses. Runs at every boot, costs one indexed
    scan of a small table. This sweep is also the template for a
    JWT_SECRET_KEY rotation (decrypt-with-old / encrypt-with-new) - see
    crypto_at_rest's docstring for why rotation needs one.
    """
    from app.crypto_at_rest import encrypt_at_rest, is_encrypted
    from app.models import User
    converged = 0
    with get_session() as db:
        for user in db.query(User).filter(User.mfa_secret.isnot(None)).all():
            if not is_encrypted(user.mfa_secret):
                user.mfa_secret = encrypt_at_rest(user.mfa_secret)
                converged += 1
        if converged:
            db.commit()
    return converged


# -- A copy before the first one-way change (2026-10-02) ----------------------
# The first boot after an update encrypts any second-factor seed and saved
# provider key still stored plaintext, in place - and an older version cannot
# read either afterwards (the runbook's "Going back"; the 2026-09-29 upgrade
# rehearsal ran it: the old code booted healthy, then failed every second
# factor and would have sent a ciphertext as the provider key). The way back
# is a copy taken before that boot, which was a step the operator had to
# remember. Now the boot takes it: when this boot would convert anything, the
# database is copied first through SQLite's backup API (a consistent snapshot
# under a live writer, unlike a file copy), and if the copy cannot be written
# the conversion is HELD - the rows stay plaintext, readable by both versions,
# and the log says why. Decided once per boot; both sweeps ask.
#
# The copy holds the seeds and provider keys as the older version stored them -
# in plaintext, which is the point: that version reads nothing else. So it does
# not stay and does not travel (2026-10-02, the wrap's drift sweep: kept with no
# end and carried by every later backup, it undid the encryption at rest for
# exactly the leaked-archive case that encryption is for). A copy older than
# PRE_UPDATE_COPY_KEEP_DAYS is deleted at boot, the boot names each copy it
# still holds and until when, and no backup carries the directory.
PRE_UPDATE_COPY_DIR = os.getenv("PRE_UPDATE_COPY_DIR", "")   # default: <db dir>/pre-update
PRE_UPDATE_COPIES_KEPT = int(os.getenv("PRE_UPDATE_COPIES_KEPT", "3"))
PRE_UPDATE_COPY_KEEP_DAYS = int(os.getenv("PRE_UPDATE_COPY_KEEP_DAYS", "14"))
_one_way_allowed: bool | None = None


def pre_update_dir() -> str | None:
    """Where the copies go: PRE_UPDATE_COPY_DIR, else <db dir>/pre-update.
    None when the database is not a SQLite file."""
    from sqlalchemy.engine import make_url
    if not _sqlite:
        return None
    src = make_url(DATABASE_URL).database
    if not src or src == ":memory:":
        return None
    return PRE_UPDATE_COPY_DIR or os.path.join(os.path.dirname(os.path.abspath(src)),
                                               "pre-update")


def expire_pre_update_copies(now: float | None = None) -> dict:
    """At boot: delete the copies past PRE_UPDATE_COPY_KEEP_DAYS and name the
    ones still held. A copy's age is read from the UTC time in its name, or the
    file's own time when the name carries none. {"deleted": [...], "held": [...]}."""
    import calendar
    import time
    out = {"deleted": [], "held": []}
    d = pre_update_dir()
    if not d or not os.path.isdir(d):
        return out
    now = time.time() if now is None else now
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        try:
            taken = calendar.timegm(time.strptime(name.rsplit(".", 1)[-1], "%Y%m%dT%H%M%SZ"))
        except ValueError:
            taken = os.path.getmtime(path)
        if now - taken > PRE_UPDATE_COPY_KEEP_DAYS * 86400:
            try:
                os.remove(path)
                out["deleted"].append(name)
                print(f"pre-update copy deleted: {name} - older than "
                      f"PRE_UPDATE_COPY_KEEP_DAYS ({PRE_UPDATE_COPY_KEEP_DAYS})", flush=True)
            except OSError as e:
                print(f"pre-update copy: could not delete {path} ({e})", flush=True)
        else:
            out["held"].append(name)
            until = time.strftime("%Y-%m-%d", time.gmtime(taken + PRE_UPDATE_COPY_KEEP_DAYS * 86400))
            print(f"pre-update copy held: {path} - it keeps second-factor seeds and "
                  "provider keys as the older version stored them, in plaintext; "
                  f"deleted at the first boot after {until}, or delete it once this "
                  "version is confirmed", flush=True)
    return out


def is_pre_update_dir(path: str) -> bool:
    """True for the directory that holds the pre-update copies - every walker
    of the data directory (backups, exports) skips it by asking this, so the
    walkers and the copy's home cannot disagree."""
    d = pre_update_dir()
    return bool(d) and os.path.realpath(path) == os.path.realpath(d)


def _pending_one_way() -> dict:
    """Rows this boot would rewrite in a form an older version cannot read."""
    from app.config import get_all_config, is_secret_config_key
    from app.crypto_at_rest import is_encrypted
    from app.models import User
    with get_session() as db:
        seeds = sum(1 for (s,) in db.query(User.mfa_secret)
                    .filter(User.mfa_secret.isnot(None)).all() if not is_encrypted(s))
    keys = sum(1 for k, v in get_all_config().items()
               if is_secret_config_key(k) and v and not is_encrypted(v))
    return {"mfa_seeds": seeds, "provider_keys": keys}


def take_pre_update_copy() -> str | None:
    """Copy the SQLite database before a one-way change; the copy's path, or
    None when it could not be taken (not SQLite, no file yet, or a failed
    write). The newest PRE_UPDATE_COPIES_KEPT copies are kept."""
    import sqlite3
    import time
    from sqlalchemy.engine import make_url
    if not _sqlite:
        return None
    src = make_url(DATABASE_URL).database
    if not src or src == ":memory:" or not os.path.exists(src):
        return None
    dest_dir = pre_update_dir()
    base = os.path.basename(src)
    try:
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        dest = os.path.join(dest_dir, f"{base}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
        source, target = sqlite3.connect(src), sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.chmod(dest, 0o600)
        kept = sorted(f for f in os.listdir(dest_dir) if f.startswith(base + "."))
        for old in kept[:-PRE_UPDATE_COPIES_KEPT]:
            os.remove(os.path.join(dest_dir, old))
        return dest
    except Exception as e:
        print(f"pre-update copy: could not write it ({e})", flush=True)
        return None


def one_way_changes_allowed() -> bool:
    """True when this boot may convert rows an older version cannot read:
    there is nothing to convert, or the copy was taken. Decided once per boot."""
    global _one_way_allowed
    if _one_way_allowed is None:
        pending = _pending_one_way()
        if not any(pending.values()):
            _one_way_allowed = True
        else:
            copy = take_pre_update_copy()
            _one_way_allowed = copy is not None
            if copy:
                print(f"pre-update copy: {copy} - taken before converting {pending} "
                      "in place (the way back: the runbook's Going back)", flush=True)
            else:
                print(f"one-way change HELD: {pending} stay as they are - the boot could "
                      "not copy the database first, so it does not convert them. Back the "
                      "database up, then restart.", flush=True)
    return _one_way_allowed


def init_db():
    from app import models  # noqa: F401 - register all ORM models
    Base.metadata.create_all(engine)
    _rebuild_chat_sessions_unique()
    _run_migrations()
    expire_pre_update_copies()
    encrypted = _encrypt_mfa_seeds_sweep() if one_way_changes_allowed() else 0
    if encrypted:
        print(f"mfa seed sweep: encrypted {encrypted} plaintext seed(s) at rest",
              flush=True)
    swept = sweep_fk_orphans()
    if swept["residual"]:
        print(f"fk orphan sweep: WARNING - {swept['residual']} violation(s) "
              "REMAIN after the pass bound; enforcement will surface them as "
              "write failures", flush=True)
    elif swept["deleted"] or swept["nulled"]:
        print(f"fk orphan sweep: {swept}", flush=True)
