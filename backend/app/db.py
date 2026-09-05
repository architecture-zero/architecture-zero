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
        return {"deleted": {}, "nulled": {}}
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
        raw.commit()
    finally:
        raw.close()
    return {"deleted": deleted, "nulled": nulled}


def init_db():
    from app import models  # noqa: F401 - register all ORM models
    Base.metadata.create_all(engine)
    _rebuild_chat_sessions_unique()
    _run_migrations()
    swept = sweep_fk_orphans()
    if swept["deleted"] or swept["nulled"]:
        print(f"fk orphan sweep: {swept}", flush=True)
