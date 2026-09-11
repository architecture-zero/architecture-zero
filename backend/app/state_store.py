"""Restart-proof state for the security controls (2026-09-11).

Until this module existed, every small piece of state the security controls
kept - the sliding windows behind the chat, auth and first-owner-claim
throttles, the per-challenge MFA attempt counter and its burned jtis, the
daily guest budget - lived in a Python dict inside the one uvicorn process.
Each of those dicts carried the same honest caveat: single worker, and a
restart forgets everything. That was the 2026-09-04 readiness audit's
"per-process auth stores" finding (adjudication item 6): a redeploy hands
every IP a fresh throttle budget and honours a completed or exhausted MFA
challenge afresh for what remains of its five-minute life. Redis was the
deferred answer, and it is deferred because it is about scaling - a second
node - while the restart-forgetting is a today-problem on the one node.

This is the today-answer: the database every deployment already has. SQLite
with WAL and a busy timeout serialises writers without erroring, the table is
created with every other table, and there is nothing new to run. A control
that already speaks Redis keeps that path when REDIS_URL is set; this store
replaces the in-memory fallback, never the Redis path.

Contract:
- Rows carry their own expiry. A read past `expires_at` is a miss and drops
  the row; `sweep()` drops every expired row and is called opportunistically
  from `bump_window` so the table never grows with the count of addresses
  that ever hit the instance (the same bound the old dicts had to learn).
- `version` is an optimistic-concurrency stamp: `bump_window` and
  `bump_counter` read, compute, and write only if nobody else wrote in
  between, retrying a few times otherwise. Two workers therefore cannot lose
  each other's increment - which also retires the "single worker" caveat for
  a multi-worker deployment on ONE node (a second node still needs Redis:
  its database is a different file).
- `put_if_absent` is the single-use primitive (a burned one-time token): an
  INSERT that either lands or does not, decided by the primary key.
- Values are small JSON documents. Keys are namespaced by the caller
  (`rate:`, `auth:`, `setup:`, `mfa:`, `guest_budget:`, `sso_jti:`).
"""
import json
import time

from sqlalchemy.exc import IntegrityError

from app.db import engine, get_session
from app.models import SecurityState

_SWEEP_EVERY = 200          # bump_window calls between opportunistic sweeps
_RETRIES = 6                # optimistic-concurrency attempts before giving up
_calls_since_sweep = 0
_table_ready = False


class _Conflict(Exception):
    """Another writer moved the row between our read and our write."""


def _ensure_table() -> None:
    """The table is created with the rest of the schema at boot; this makes
    the store safe to call before that (a test fixture, a fork whose boot
    order differs) and costs one cheap check per process."""
    global _table_ready
    if not _table_ready:
        SecurityState.__table__.create(engine, checkfirst=True)
        _table_ready = True


def _now() -> float:
    return time.time()


def get(key: str) -> dict | None:
    """The stored document, or None when absent or expired (an expired row is
    dropped on the way out)."""
    _ensure_table()
    with get_session() as db:
        row = db.get(SecurityState, key)
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at <= _now():
            db.delete(row)
            return None
        return json.loads(row.value)


def put(key: str, value: dict, ttl: float, *, keep_expiry: bool = False) -> None:
    """Upsert `value` under `key`, expiring `ttl` seconds from now - or, with
    keep_expiry, keeping the expiry an existing row already has (a challenge
    that must die with the token it tracks, not later)."""
    _ensure_table()
    now = _now()
    with get_session() as db:
        row = db.get(SecurityState, key)
        if row is None:
            db.add(SecurityState(key=key, value=json.dumps(value),
                                 expires_at=now + ttl, version=1))
            return
        row.value = json.dumps(value)
        if not keep_expiry or row.expires_at is None or row.expires_at <= now:
            row.expires_at = now + ttl
        row.version = (row.version or 0) + 1


def put_if_absent(key: str, value: dict, ttl: float) -> bool:
    """Insert only if no live row exists. True when this call created it -
    the one-time-token primitive: exactly one redeemer sees True."""
    _ensure_table()
    now = _now()
    try:
        with get_session() as db:
            row = db.get(SecurityState, key)
            if row is not None:
                if row.expires_at is not None and row.expires_at <= now:
                    db.delete(row)
                    db.flush()
                else:
                    return False
            db.add(SecurityState(key=key, value=json.dumps(value),
                                 expires_at=now + ttl, version=1))
        return True
    except IntegrityError:
        return False   # a concurrent redeemer inserted first


def delete(key: str) -> None:
    _ensure_table()
    with get_session() as db:
        row = db.get(SecurityState, key)
        if row is not None:
            db.delete(row)


def bump_window(key: str, window: float, limit: int) -> tuple[int, bool]:
    """Sliding-window attempt counter. Counts the timestamps inside the last
    `window` seconds; when that count is below `limit` the call is allowed
    and `now` is recorded, otherwise nothing is recorded (a refused attempt
    does not extend the window, and the row still expires with it).
    Returns (count_before_this_call, allowed)."""
    global _calls_since_sweep
    _ensure_table()
    now = _now()
    cutoff = now - window
    _calls_since_sweep += 1
    if _calls_since_sweep >= _SWEEP_EVERY:
        _calls_since_sweep = 0
        sweep(now)
    for _ in range(_RETRIES):
        try:
            with get_session() as db:
                row = db.get(SecurityState, key)
                if row is None or (row.expires_at is not None and row.expires_at <= now):
                    stamps: list[float] = []
                    version = None if row is None else row.version
                else:
                    stamps = [t for t in json.loads(row.value).get("ts", []) if t > cutoff]
                    version = row.version
                allowed = len(stamps) < limit
                new = stamps + ([now] if allowed else [])
                value = json.dumps({"ts": new})
                if row is None:
                    db.add(SecurityState(key=key, value=value,
                                         expires_at=now + window, version=1))
                else:
                    n = (db.query(SecurityState)
                           .filter(SecurityState.key == key,
                                   SecurityState.version == version)
                           .update({"value": value,
                                    "expires_at": now + window,
                                    "version": (version or 0) + 1},
                                   synchronize_session=False))
                    if n != 1:
                        raise _Conflict()
            return len(stamps), allowed
        except (IntegrityError, _Conflict):
            continue
    raise RuntimeError(f"state store: could not settle {key!r} after {_RETRIES} attempts")


def bump_counter(key: str, ttl: float) -> int:
    """Increment a plain counter and return the new value. The expiry is set
    when the row is created and kept after (a daily budget dies at its day,
    not `ttl` after its last hit)."""
    _ensure_table()
    now = _now()
    for _ in range(_RETRIES):
        try:
            with get_session() as db:
                row = db.get(SecurityState, key)
                if row is None or (row.expires_at is not None and row.expires_at <= now):
                    if row is not None:
                        db.delete(row)
                        db.flush()
                    db.add(SecurityState(key=key, value=json.dumps({"n": 1}),
                                         expires_at=now + ttl, version=1))
                    return 1
                count = int(json.loads(row.value).get("n", 0)) + 1
                n = (db.query(SecurityState)
                       .filter(SecurityState.key == key,
                               SecurityState.version == row.version)
                       .update({"value": json.dumps({"n": count}),
                                "version": (row.version or 0) + 1},
                               synchronize_session=False))
                if n != 1:
                    raise _Conflict()
            return count
        except (IntegrityError, _Conflict):
            continue
    raise RuntimeError(f"state store: could not settle {key!r} after {_RETRIES} attempts")


def update(key: str, mutate, ttl: float, *, default: dict,
           keep_expiry: bool = True) -> dict:
    """Read-modify-write of one document under optimistic concurrency:
    `mutate(doc)` edits the live document in place (or `default` when the
    row is absent or expired), and the write lands only if nobody else wrote
    in between - retried otherwise, so two workers cannot lose each other's
    change. With keep_expiry (the default) an existing live row keeps its
    expiry; a new or expired row gets `ttl` from now. Returns the document."""
    _ensure_table()
    now = _now()
    for _ in range(_RETRIES):
        try:
            with get_session() as db:
                row = db.get(SecurityState, key)
                live = row is not None and not (
                    row.expires_at is not None and row.expires_at <= now)
                doc = json.loads(row.value) if live else dict(default)
                mutate(doc)
                value = json.dumps(doc)
                if row is None:
                    db.add(SecurityState(key=key, value=value,
                                         expires_at=now + ttl, version=1))
                else:
                    fields = {"value": value, "version": (row.version or 0) + 1}
                    if not live or not keep_expiry:
                        fields["expires_at"] = now + ttl
                    n = (db.query(SecurityState)
                           .filter(SecurityState.key == key,
                                   SecurityState.version == row.version)
                           .update(fields, synchronize_session=False))
                    if n != 1:
                        raise _Conflict()
            return doc
        except (IntegrityError, _Conflict):
            continue
    raise RuntimeError(f"state store: could not settle {key!r} after {_RETRIES} attempts")


def sweep(now: float | None = None) -> int:
    """Drop every expired row. Returns how many went."""
    _ensure_table()
    now = _now() if now is None else now
    with get_session() as db:
        return (db.query(SecurityState)
                  .filter(SecurityState.expires_at.isnot(None),
                          SecurityState.expires_at <= now)
                  .delete(synchronize_session=False))


def clear(prefix: str = "") -> int:
    """Drop every row (or every row under `prefix`). Tests and operators."""
    _ensure_table()
    with get_session() as db:
        q = db.query(SecurityState)
        if prefix:
            q = q.filter(SecurityState.key.like(prefix.replace("%", r"\%") + "%"))
        return q.delete(synchronize_session=False)


def keys(prefix: str = "") -> list[str]:
    """Live keys under `prefix` - for tests and the operator's eye."""
    _ensure_table()
    now = _now()
    with get_session() as db:
        q = db.query(SecurityState.key, SecurityState.expires_at)
        if prefix:
            q = q.filter(SecurityState.key.like(prefix.replace("%", r"\%") + "%"))
        return [k for k, exp in q.all() if exp is None or exp > now]
