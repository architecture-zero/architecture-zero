"""Password auth, JWT minting/validation, and the password policy.

Access tokens are short-lived JWTs; refresh tokens are opaque random values
stored only as hashes. Any token carrying a "type" claim (e.g. the MFA
challenge token) is rejected as a Bearer credential - a challenge token must
never be replayable as an access token just because both are signed with the
same secret.
"""
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer
from jose import JWTError, jwt

from app.permissions import effective_permissions, is_owner
from app.users import get_user_by_id, get_user_by_username

# Password policy. Applies to NEW passwords only (validated at setup, user
# creation, and password change - never at login), so tightening it can not
# lock an existing account out.
MIN_PASSWORD_LENGTH   = int(os.getenv("MIN_PASSWORD_LENGTH", "12"))
REQUIRE_SPECIAL_CHARS = os.getenv("REQUIRE_SPECIAL_CHARS", "true").lower() == "true"
REQUIRE_UPPERCASE     = os.getenv("REQUIRE_UPPERCASE", "true").lower() == "true"

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-before-deploying")
ALGORITHM = "HS256"
ACCESS_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_EXPIRE_MINUTES", "30"))
REFRESH_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_EXPIRE_DAYS", "7"))

# auto_error=False: routes that allow anonymous access (e.g. a guest-gated
# chat endpoint) resolve their user optionally; enforcing routes go through
# get_current_user below, which raises on missing credentials itself.
oauth2_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(user_id: int, username: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "role": role, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def create_refresh_token(user_id: int) -> tuple[str, str]:
    """Returns (raw_token, expires_at_iso). Store only the HASH of the raw
    token - a database read must never yield a usable credential."""
    import secrets
    raw = secrets.token_urlsafe(48)
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_EXPIRE_DAYS)
    return raw, expire.isoformat()


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Reject ANY typed token: access tokens carry no "type" claim, so a
    # purpose-bound token (MFA challenge, or any future handoff token minted
    # under this secret) can never be replayed as a Bearer credential.
    if payload.get("type") is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def validate_password(password: str) -> list[str]:
    """Return a list of policy violations. Empty list = valid."""
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if REQUIRE_UPPERCASE and not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter")
    if REQUIRE_SPECIAL_CHARS and not any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?" for c in password):
        errors.append("Password must contain at least one special character")
    return errors


def create_mfa_challenge_token(user_id: int) -> str:
    """Short-lived, purpose-bound JWT that carries identity between the
    password step and TOTP verification. The "type" claim is what stops it
    from doubling as an access token (see decode_access_token)."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=5)
    return jwt.encode(
        {"sub": str(user_id), "type": "mfa", "jti": uuid.uuid4().hex, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def decode_mfa_challenge_token(token: str) -> tuple[int, str]:
    """Verify an MFA challenge token and return (user_id, jti). Raises 401 on any
    failure - including a plain access token presented in its place.

    Returns a TUPLE since 2026-08-27: the caller needs the jti to enforce
    single-use and per-challenge attempt limits. A token with no jti is REFUSED
    rather than accepted uncounted - pre-fix tokens can be neither burned nor
    bounded, so honouring them would leave exactly the hole this closes. Blast
    radius is one challenge-lifetime window at deploy.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired MFA token")
    if payload.get("type") != "mfa":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    jti = payload.get("jti")
    if not jti:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired MFA token")
    return int(payload["sub"]), str(jti)


def authenticate_user(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


async def get_current_user(credentials=Depends(oauth2_scheme)) -> dict:
    """The enforcing dependency: raises 401 on missing or invalid
    credentials. Used route-level on every non-public route (a wiring test
    sweeps app.routes to keep that true), so authorization holds even if the
    middleware layer is ever disabled."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_access_token(credentials.credentials)
    user_id = int(payload.get("sub", 0))
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def require_owner(current_user: dict = Depends(get_current_user)) -> dict:
    """Owner-only. Guards system/ops endpoints - model settings, backups,
    eval internals - that admins must not reach."""
    if not is_owner(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required")
    return current_user


def require_permission(scope: str):
    """Dependency factory: passes for the Owner (full bypass) or any user
    whose resolved permissions include the scope."""
    async def _check(current_user: dict = Depends(get_current_user)) -> dict:
        if is_owner(current_user):
            return current_user
        if scope not in effective_permissions(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {scope}",
            )
        return current_user
    return _check
