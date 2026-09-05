"""SQLite FK enforcement (2026-09-04, fleet-wide alongside az-personal).

SQLite ships with foreign-key enforcement OFF, so a ForeignKey declared in
models.py was decorative - a deleted parent silently orphans its children,
and sqlite id-reuse can then cross-wire an orphan onto a brand-new row (the
az-personal connector incident). This surface declares no ondelete rules, so
what enforcement buys here is refusing NEW orphans; the boot sweep clears any
the unenforced past already left.
"""
import pytest
from sqlalchemy import text

from app.db import engine, get_session, sweep_fk_orphans


def test_app_connections_enforce_foreign_keys(client):
    """Positive signal, not absence-of-error: read the pragma back."""
    with get_session() as db:
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_dangling_insert_is_refused(client):
    from sqlalchemy.exc import IntegrityError
    from app.models import RefreshToken

    with pytest.raises(IntegrityError):
        with get_session() as db:
            db.add(RefreshToken(user_id=99999999, token_hash="fk-dangling",
                                expires_at="2026-09-05T00:00:00"))


def test_sweep_clears_planted_orphans(client):
    """Plant an orphan the way the unenforced past created them - on a direct
    sqlite3 connection, which is FK-off by default AND outside the engine
    pool, so nothing about this test leaks pragma state into the pool."""
    import sqlite3
    raw = sqlite3.connect(engine.url.database)
    try:
        cur = raw.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _fk_probe_parent "
                    "(id INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE IF NOT EXISTS _fk_probe_child "
                    "(id INTEGER PRIMARY KEY, p_id INTEGER NOT NULL "
                    "REFERENCES _fk_probe_parent(id) ON DELETE CASCADE)")
        cur.execute("DELETE FROM _fk_probe_child")
        cur.execute("INSERT INTO _fk_probe_child (id, p_id) VALUES (1, 424242)")
        raw.commit()
    finally:
        raw.close()
    try:
        swept = sweep_fk_orphans()
        assert swept["deleted"].get("_fk_probe_child") == 1
        with get_session() as db:
            assert db.execute(
                text("SELECT count(*) FROM _fk_probe_child")).scalar() == 0
    finally:
        raw = sqlite3.connect(engine.url.database)
        cur = raw.cursor()
        cur.execute("DROP TABLE IF EXISTS _fk_probe_child")
        cur.execute("DROP TABLE IF EXISTS _fk_probe_parent")
        raw.commit()
        raw.close()
