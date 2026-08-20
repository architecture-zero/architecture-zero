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
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
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
    # Empty in the core chassis - instance forks append their own idempotent
    # column additions here for tables that predate a schema change.
    stmts: list[str] = []
    with engine.connect() as conn:
        for sql in stmts:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # column already exists


def init_db():
    from app import models  # noqa: F401 - register all ORM models
    Base.metadata.create_all(engine)
    _run_migrations()
