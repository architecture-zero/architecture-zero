"""The restore flag: a database put back from an archive keeps no session.

WHY (2026-11-17). A restore replaces the database whole, so its session
tables come back as the archive held them: every token revoked after the
archive was taken - a sign-out, a sign-out everywhere, a password change or
reset, a deactivation, a reuse-detection revoke - is live again until its
own expiry. Nothing at boot could tell a restored database from the one it
replaced. The restore can: every restore path that puts a database back
writes `restored.json` into the data directory - the directory that holds
`force-rebuild.json`, the vector store's own one-shot lever - before the
backend starts, and the next boot reads it once, right after the schema
step and before anything is served:

  - every session this surface keeps is revoked, in one transaction: the
    sign-in's refresh tokens, and the OAuth tokens an MCP client holds on a
    surface that issues them (_SESSION_TABLES). Everyone signs in again.
  - it is recorded once, as counts: a security_events row on a surface that
    keeps that table (app.audit.record_security_event), and the log line
    everywhere - on a surface without the table the log line is the record.
  - the flag is deleted, so it fires exactly once.

What the flag holds - the time and the archive's name - goes to the log
line and decides nothing; none of it is secret. Whoever can write the data
directory can already replace the database, so the flag hands nobody a new
lever: the most a planted one does is sign everyone out once.

EVERY FAILURE GOES TOWARD ONE MORE SIGN-IN, never toward a revived session:
  - a flag that is not a small JSON object still revokes, and is still
    deleted;
  - a revoke that fails keeps the flag, so the next boot tries again, and
    says so at ERROR;
  - a flag that cannot be deleted revokes again at every boot until someone
    removes it, and every one of those boots says so at ERROR.
It never raises: a boot that died here would take the instance down over a
file that exists only to make it safer.

A boot with no flag reads one path and returns.

A cached copy of a token (Redis, where it is on) needs nothing here: the
refresh route's rotation consumes the token with one UPDATE that requires
the database row to be live, so the database decides and a cached record
cannot mint a successor.
"""
import json
import os

from app.logger import log, log_error

RESTORE_FLAG_FILE = "restored.json"

# Every table of sessions a surface may keep, each with a `revoked` column.
# A surface without one of them skips it - read from the schema this boot
# has just settled - so this file is the same text on every surface.
# Authorization codes are not on the list: one lives two minutes and is
# bound to its client's PKCE verifier, so no archive holds one still good.
_SESSION_TABLES = ("refresh_tokens", "oauth_tokens")

# The flag holds two short fields. Anything larger is not a flag this
# platform wrote, and is read as unreadable - which still revokes.
_MAX_FLAG_BYTES = 4096
_CLIP = 120


def flag_path(data_dir: str | None = None) -> str:
    """Where the flag is read: the data directory force-rebuild.json is read
    from, unless a caller names another."""
    if data_dir is None:
        from app.database import _CHROMA_PATH
        data_dir = _CHROMA_PATH
    return os.path.join(os.path.abspath(data_dir), RESTORE_FLAG_FILE)


def _read_flag(path: str) -> dict | None:
    """The flag's object, or None when it is not one. Logged when not."""
    try:
        # Only a regular file is opened: a pipe or a device in its place
        # would hold the boot on the open itself.
        if not os.path.isfile(path):
            raise ValueError("not a regular file")
        with open(path, "rb") as f:
            raw = f.read(_MAX_FLAG_BYTES + 1)
        if len(raw) > _MAX_FLAG_BYTES:
            raise ValueError(f"larger than {_MAX_FLAG_BYTES} bytes")
        note = json.loads(raw.decode("utf-8"))
        if not isinstance(note, dict):
            raise ValueError(f"expected a JSON object, got {type(note).__name__}")
        return note
    except Exception as e:
        log_error("restore_flag_unreadable", error=f"{type(e).__name__}: {e}"[:300],
                  note="every session is revoked all the same")
        return None


def _clip(value) -> str | None:
    return value[:_CLIP] if isinstance(value, str) else None


def _revoke_every_session() -> dict:
    """{table: rows revoked} for every session table this surface keeps, in
    one transaction - all of them or none."""
    from app import models  # noqa: F401 - every table registered on Base
    from app.db import Base, engine
    counts = {}
    with engine.begin() as conn:
        for name in _SESSION_TABLES:
            table = Base.metadata.tables.get(name)
            if table is None:
                continue
            done = conn.execute(table.update()
                                .where(table.c.revoked == False)  # noqa: E712
                                .values(revoked=True))
            counts[name] = int(done.rowcount or 0)
    return counts


def consume_restore_flag(data_dir: str | None = None) -> dict | None:
    """Once per boot, after the schema step (app.main). None when there is
    no flag; otherwise what this boot did about it. Never raises."""
    try:
        path = flag_path(data_dir)
        if not os.path.lexists(path):
            return None
        note = _read_flag(path)
        try:
            counts = _revoke_every_session()
        except Exception as e:
            log_error("sessions_revoke_after_restore_failed",
                      error=f"{type(e).__name__}: {e}"[:300],
                      note=("the restore flag is kept, so the next boot revokes "
                            "again; until one does, sessions the archive carried "
                            "are live"))
            return {"revoked": False, "flag_kept": True}
        total = sum(counts.values())
        try:
            from app import audit
            if hasattr(audit, "record_security_event"):
                audit.record_security_event("sessions_revoked_after_restore",
                                            detail={"tokens_revoked": total})
        except Exception as e:
            # The revoke stands; only its row is lost, and the line below
            # still records it.
            log_error("sessions_revoked_after_restore_unrecorded",
                      error=f"{type(e).__name__}: {e}"[:300])
        log("sessions_revoked_after_restore", tokens_revoked=total,
            tables=dict(counts),
            flag_readable=note is not None,
            restored_at=_clip(note.get("restored_at")) if note else None,
            archive=_clip(note.get("archive")) if note else None)
        removed = True
        try:
            os.remove(path)
        except OSError as e:
            removed = False
            log_error("restore_flag_not_removed",
                      error=f"{type(e).__name__}: {e}"[:300],
                      note=("every boot revokes every session again until "
                            f"{RESTORE_FLAG_FILE} is removed from the data directory"))
        return {"revoked": True, "tokens_revoked": total, "tables": counts,
                "flag_readable": note is not None, "flag_removed": removed}
    except Exception as e:
        log_error("restore_flag_crashed", error=f"{type(e).__name__}: {e}"[:300],
                  note="the flag, if any, is left for the next boot")
        return None
