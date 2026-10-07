"""User accounts, MFA state, lockout, and refresh-token sessions.

Refresh tokens are stored as hashes and optionally cached in Redis with a
TTL. Anywhere a token is revoked, the cache entry must be dropped too -
reads consult the cache FIRST, so a DB-only revoke would leave a signed-out
session valid until its TTL expired.
"""
import json
from datetime import datetime, timezone

from app.crypto_at_rest import encrypt_at_rest, try_decrypt_at_rest
from app.db import get_session
from app.models import RefreshToken, User


def _user_to_dict(user: User) -> dict:
    # Decrypted BEFORE the literal because the answer is a pair - the seed,
    # and whether it could be read at all. See the mfa_secret entry below.
    mfa_seed, mfa_seed_readable = try_decrypt_at_rest(user.mfa_secret)
    return {
        "id": user.id,
        "username": user.username,
        "password_hash": user.password_hash,
        "role": user.role,
        "permissions": json.loads(user.permissions or "{}"),
        "department": user.department,
        "is_active": user.is_active,
        "created_at": user.created_at,
        # Stored encrypted at rest; every consumer (pyotp) needs the seed
        # itself, so the one read seam decrypts. Legacy plaintext rows pass
        # through until the migration sweep converges them.
        #
        # TOLERANT, and it degrades CLOSED (T9 - upstream 2026-09-06, fleet
        # port 2026-09-10). This function is on the path of login, of
        # get_user_by_id on EVERY authenticated request, and of list_users for
        # the admin roster, so a raise here is not one broken field - it is
        # HTTP 500 on the whole instance for that account with no reason
        # anywhere on screen, and ONE stranded row takes the roster down for
        # every operator. That is what a restore under a different
        # JWT_SECRET_KEY produced. The seed reads as None because there is no
        # partially usable seed, and mfa_secret_unreadable carries the REASON
        # so a caller can tell "never enrolled" (None, False) from "second
        # factor exists and cannot be read" (None, True). Every path that
        # mints a session MUST refuse on the second -
        # jwt_auth.refuse_if_mfa_seed_stranded - or anyone able to write
        # garbage into this one column has stripped the second factor.
        "mfa_secret": mfa_seed,
        "mfa_secret_unreadable": not mfa_seed_readable,
        "mfa_enabled": user.mfa_enabled,
        "failed_attempts": user.failed_attempts,
        "locked_until": user.locked_until,
    }


def create_user(username: str, password_hash: str, role: str = "member",
                department: str = "general") -> int:
    with get_session() as db:
        user = User(
            username=username,
            password_hash=password_hash,
            role=role,
            permissions="{}",
            department=department,
            created_at=datetime.utcnow().isoformat(),
        )
        db.add(user)
        db.flush()
        return user.id


def get_user_by_username(username: str) -> dict | None:
    with get_session() as db:
        user = db.query(User).filter(User.username == username,
                                     User.is_active == True).first()  # noqa: E712
        return _user_to_dict(user) if user else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_session() as db:
        user = db.query(User).filter(User.id == user_id,
                                     User.is_active == True).first()  # noqa: E712
        return _user_to_dict(user) if user else None


def list_users() -> list[dict]:
    with get_session() as db:
        users = db.query(User).order_by(User.id).all()
        return [_user_to_dict(u) for u in users]


def deactivate_user(user_id: int):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update({"is_active": False})


def update_user_role(user_id: int, role: str):
    # A role change resets the account's explicit permission list to "{}" -
    # the new role's preset, what a new account stores - in the same write
    # (2026-09-22). A stored list REPLACES the preset (effective_permissions),
    # so kept across a role change it held authority the new role withholds
    # (an Admin with a list, demoted, kept manage_users) or lacked what the
    # new role grants. Extras are granted again explicitly after the change.
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update({"role": role, "permissions": "{}"})


def update_user_department(user_id: int, department: str):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update({"department": department})


def update_user_permissions(user_id: int, permissions: list[str]):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"permissions": json.dumps(permissions)})


def owner_exists() -> bool:
    """Has the one-time setup been done? True once an Owner account exists -
    gates the public /api/auth/setup bootstrap so it can't mint a second
    superuser."""
    with get_session() as db:
        return db.query(User).filter(User.role == "owner",
                                     User.is_active == True).count() > 0  # noqa: E712


CLAIM_MARKER = "setup:claimed"


class ClaimLost(Exception):
    """A concurrent claim committed the first Owner first."""


def claim_first_owner(username: str, password_hash: str) -> int:
    """Create the first Owner - exactly once (AZ-04, outside review 2026-10-06;
    fixed 2026-10-07).

    The setup route checked owner_exists(), verified the claim code, created
    the account, then burned the code: separate steps with no single winner,
    so two valid claims that both passed the check made two Owners - and the
    code is minted per process, so on two workers there are two valid codes.
    Now the Owner and a durable claim marker (the security_state row
    CLAIM_MARKER) are written in ONE transaction, and the Owner check is
    repeated inside it. The marker's primary key is the single winner, held
    by the database across workers and restarts: the loser's insert fails and
    its account rolls back with it.

    The marker outlives the claim on purpose. A deployment whose Owners were
    all removed by hand stays closed until the operator deletes the marker
    too - claimed once is claimed. Raises ClaimLost to the loser, and
    IntegrityError when the username is taken.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError
    from app.models import SecurityState
    with get_session() as db:
        if db.query(User).filter(User.role == "owner",
                                 User.is_active == True).first():  # noqa: E712 - owner_exists()'s test
            raise ClaimLost()
        db.add(SecurityState(key=CLAIM_MARKER, version=1, expires_at=None,
                             value=json.dumps({"claimed_at": datetime.now(timezone.utc).isoformat()})))
        try:
            db.flush()
        except IntegrityError:
            raise ClaimLost()
        except OperationalError:
            # SQLite: a claim committed after this transaction's first read,
            # and a write cannot proceed on that stale snapshot. Lost to that
            # claim if an Owner exists now; anything else is a real failure.
            if owner_exists():
                raise ClaimLost()
            raise
        user = User(username=username, password_hash=password_hash, role="owner",
                    permissions="{}", department="general",
                    created_at=datetime.utcnow().isoformat())
        db.add(user)
        db.flush()          # IntegrityError: the username is taken
        return user.id


def count_active_owners() -> int:
    """Active Owner accounts. Protects the LAST Owner: deactivating or
    demoting it would drop owner_exists() to false and re-open public setup."""
    with get_session() as db:
        return db.query(User).filter(User.role == "owner",
                                     User.is_active == True).count()  # noqa: E712


def update_user_password(user_id: int, password_hash: str):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update({"password_hash": password_hash})


def update_user_username(user_id: int, username: str) -> bool:
    with get_session() as db:
        existing = db.query(User).filter(User.username == username,
                                         User.id != user_id).first()
        if existing:
            return False
        db.query(User).filter(User.id == user_id).update({"username": username})
        return True


# ── MFA ──────────────────────────────────────────────────────────────────────

def set_mfa_secret(user_id: int, secret: str):
    # Enabling is a separate step: the secret is provisional until the user
    # proves possession with a first valid code. Encrypted at rest - the
    # column must never hold a mintable seed in the clear.
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"mfa_secret": encrypt_at_rest(secret), "mfa_enabled": False})


def enable_mfa(user_id: int):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update({"mfa_enabled": True})


def disable_mfa(user_id: int):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"mfa_secret": None, "mfa_enabled": False})


# ── Account lockout ──────────────────────────────────────────────────────────

def increment_failed_attempts(user_id: int) -> int:
    with get_session() as db:
        user = db.query(User).filter(User.id == user_id).one()
        user.failed_attempts += 1
        db.flush()
        return user.failed_attempts


def reset_failed_attempts(user_id: int):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"failed_attempts": 0, "locked_until": None})


def lock_user(user_id: int, until_iso: str):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update({"locked_until": until_iso})


def unlock_user(user_id: int):
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"failed_attempts": 0, "locked_until": None})


# ── Refresh tokens ───────────────────────────────────────────────────────────

def _rt_redis_key(token_hash: str) -> str:
    return f"az:rt:{token_hash}"


def store_refresh_token(user_id: int, token_hash: str, expires_at: str):
    with get_session() as db:
        db.add(RefreshToken(user_id=user_id, token_hash=token_hash,
                            expires_at=expires_at))
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        try:
            expires_dt = datetime.fromisoformat(expires_at)
            ttl = int((expires_dt - datetime.now(timezone.utc)).total_seconds())
            if ttl > 0:
                r.setex(
                    _rt_redis_key(token_hash),
                    ttl,
                    json.dumps({"user_id": user_id, "expires_at": expires_at,
                                "revoked": 0}),
                )
        except Exception:
            pass


def get_refresh_token(token_hash: str) -> dict | None:
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        try:
            val = r.get(_rt_redis_key(token_hash))
            if val is not None:
                return json.loads(val)
        except Exception:
            pass
    with get_session() as db:
        rt = db.query(RefreshToken).filter(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False  # noqa: E712
        ).first()
        if not rt:
            return None
        return {"id": rt.id, "user_id": rt.user_id,
                "expires_at": rt.expires_at, "revoked": rt.revoked}


def _redis_delete_failed(where: str, err: Exception) -> None:
    """A swallowed Redis delete on a REVOCATION path is the one condition under
    which a revoked refresh token keeps working (get_refresh_token serves the
    cached record without re-checking the DB flag, up to the token TTL) and
    reuse detection never fires (the normal lookup hits, so the ghost check
    is never reached). Still non-fatal - the DB stays authoritative and a
    Redis blip must not fail a logout - but LOUD, so the inconsistency window
    is visible instead of silent (fleet port 2026-09-10; upstream's
    2026-09-05 security review, finding 1)."""
    from app.logger import log
    log("refresh_redis_delete_failed", where=where, error=str(err)[:200])


def rotate_refresh_token(old_hash: str, user_id: int, new_hash: str,
                         new_expires_at: str) -> bool:
    """Consume a live refresh token and store its successor - ONE authoritative
    step (AZ-01, outside review 2026-10-06; fixed 2026-10-07).

    The refresh route read the token, revoked it unconditionally, then minted
    and stored a successor, so two requests that both read the live token
    before either revoked it each got a successor: a single-use credential
    yielded two, including when a stolen copy raced its owner. Now the consume
    is one UPDATE whose WHERE requires the row to be live, unexpired and this
    user's; only the request whose UPDATE changed the row (rowcount 1) stores a
    successor, in the same transaction. The database decides - a cached record
    is a lookup aid, never the authority. False tells the loser it lost: the
    token was already consumed, which the caller treats as reuse."""
    from sqlalchemy import update
    now = datetime.now(timezone.utc).isoformat()
    with get_session() as db:
        won = db.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == old_hash,
                   RefreshToken.user_id == user_id,
                   RefreshToken.revoked == False,  # noqa: E712
                   RefreshToken.expires_at > now)
            .values(revoked=True)).rowcount
        if won != 1:
            return False
        db.add(RefreshToken(user_id=user_id, token_hash=new_hash,
                            expires_at=new_expires_at))
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        try:
            r.delete(_rt_redis_key(old_hash))
        except Exception as e:
            _redis_delete_failed("rotate_refresh_token", e)
        try:
            ttl = int((datetime.fromisoformat(new_expires_at)
                       - datetime.now(timezone.utc)).total_seconds())
            if ttl > 0:
                r.setex(_rt_redis_key(new_hash), ttl,
                        json.dumps({"user_id": user_id, "expires_at": new_expires_at,
                                    "revoked": 0}))
        except Exception:
            pass
    return True


def get_refresh_token_any(token_hash: str) -> dict | None:
    """The row for this hash INCLUDING revoked ones - the reuse-detection read
    (fleet port 2026-09-10; upstream's auth-gaps batch, 2026-09-05).
    Deliberately DB-only: revoke deletes the Redis key outright, so Redis
    cannot distinguish 'rotated and replayed' from 'never existed', and this
    lookup only runs after the normal (revoked-filtered) one missed."""
    with get_session() as db:
        rt = db.query(RefreshToken).filter(
            RefreshToken.token_hash == token_hash).first()
        if not rt:
            return None
        return {"id": rt.id, "user_id": rt.user_id,
                "expires_at": rt.expires_at, "revoked": rt.revoked}


def revoke_all_user_tokens(user_id: int):
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        try:
            with get_session() as db:
                # EVERY row of the user, revoked or not (2026-09-10 T9 review): a
                # row flagged earlier by a DB-only revoke can still be cached, and
                # a filter on the flag would skip exactly that key.
                hashes = [rt.token_hash for rt in db.query(RefreshToken).filter(
                    RefreshToken.user_id == user_id
                ).all()]
            if hashes:
                r.delete(*[_rt_redis_key(h) for h in hashes])
        except Exception as e:
            _redis_delete_failed("revoke_all_user_tokens", e)
    with get_session() as db:
        db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id).update({"revoked": True})


# ── Session listing ──────────────────────────────────────────────────────────

def list_user_sessions(user_id: int) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    with get_session() as db:
        rows = db.query(RefreshToken).filter(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked == False,  # noqa: E712
            RefreshToken.expires_at > now,
        ).all()
        return [{"id": rt.id, "expires_at": rt.expires_at} for rt in rows]


def revoke_refresh_token_by_id(token_id: int, user_id: int):
    # Look up the hash, flip the DB flag, THEN drop the cache entry - reads
    # consult the cache first and never re-check the DB revoked flag, so a
    # DB-only revoke here would leave a "signed-out" session valid until its
    # TTL expired.
    from app.redis_client import get_redis
    with get_session() as db:
        rt = db.query(RefreshToken).filter(
            RefreshToken.id == token_id, RefreshToken.user_id == user_id
        ).first()
        if not rt:
            return
        token_hash = rt.token_hash
        rt.revoked = True
    r = get_redis()
    if r:
        try:
            r.delete(_rt_redis_key(token_hash))
        except Exception as e:
            _redis_delete_failed("revoke_refresh_token_by_id", e)
