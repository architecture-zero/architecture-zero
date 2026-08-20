"""User accounts, MFA state, lockout, and refresh-token sessions.

Refresh tokens are stored as hashes and optionally cached in Redis with a
TTL. Anywhere a token is revoked, the cache entry must be dropped too -
reads consult the cache FIRST, so a DB-only revoke would leave a signed-out
session valid until its TTL expired.
"""
import json
from datetime import datetime, timezone

from app.db import get_session
from app.models import RefreshToken, User


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "password_hash": user.password_hash,
        "role": user.role,
        "permissions": json.loads(user.permissions or "{}"),
        "department": user.department,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "mfa_secret": user.mfa_secret,
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
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update({"role": role})


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
    # proves possession with a first valid code.
    with get_session() as db:
        db.query(User).filter(User.id == user_id).update(
            {"mfa_secret": secret, "mfa_enabled": False})


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


def revoke_refresh_token(token_hash: str):
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        try:
            r.delete(_rt_redis_key(token_hash))
        except Exception:
            pass
    with get_session() as db:
        db.query(RefreshToken).filter(
            RefreshToken.token_hash == token_hash).update({"revoked": True})


def revoke_all_user_tokens(user_id: int):
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        try:
            with get_session() as db:
                hashes = [rt.token_hash for rt in db.query(RefreshToken).filter(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked == False  # noqa: E712
                ).all()]
            if hashes:
                r.delete(*[_rt_redis_key(h) for h in hashes])
        except Exception:
            pass
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
        except Exception:
            pass
