"""Password auth, JWT minting/validation, and the password policy.

Access tokens are short-lived JWTs; refresh tokens are opaque random values
stored only as hashes. Any token carrying a "type" claim (e.g. the MFA
challenge token) is rejected as a Bearer credential - a challenge token must
never be replayable as an access token just because both are signed with the
same secret.
"""
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer
from jose import JWTError, jwt

from app.permissions import effective_permissions, is_owner, STEP_UP_SCOPES
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

# The per-account lockout knob. ONE env read for the three readers - login,
# mfa/complete (both in routers/auth.py, which re-exports these names) and the
# step-up below - because two lockout rules on one account is how one of them
# ends up being the weaker one nobody remembers. Tests monkeypatch
# routers/auth.py's binding for the login path.
MAX_LOGIN_ATTEMPTS       = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_DURATION_MINUTES = int(os.getenv("LOCKOUT_DURATION_MINUTES", "15"))

# -- Unusable passwords (the sentinel, ported from upstream 2026-09-09) -------
# Upstream's SSO-provisioned accounts have no password anyone knows; they used
# to carry bcrypt(random), unguessable but indistinguishable from a real hash,
# so no rule could be ENFORCED on them. The sentinel is not a bcrypt string
# at all (Django's "!" pattern): verify_password refuses it without calling
# bcrypt, has_usable_password can see it, and the role/permission writers
# refuse to hand such an account manage_users or manage_system. This surface
# has no SSO today; the rule rides here so a port inherits it.
UNUSABLE_PASSWORD_PREFIX = "!"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def unusable_password_hash(reason: str = "no-password") -> str:
    """A password_hash value NO password can satisfy, marked as such."""
    return f"{UNUSABLE_PASSWORD_PREFIX}{reason}:{secrets.token_urlsafe(24)}"


def has_usable_password(user_or_hash) -> bool:
    """False for the sentinel above (and for an empty column)."""
    hashed = (user_or_hash.get("password_hash") if isinstance(user_or_hash, dict)
              else user_or_hash)
    return bool(hashed) and not str(hashed).startswith(UNUSABLE_PASSWORD_PREFIX)


def verify_password(plain: str, hashed: str) -> bool:
    # The sentinel is not a bcrypt string; checkpw would raise "Invalid salt"
    # and turn that account's login into a 500. Refuse it plainly.
    if not has_usable_password(hashed):
        return False
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def password_required_for_authority(target: dict | None, scopes) -> str | None:
    """An account with no usable password may not hold manage_users or
    manage_system. Returns the refusal (naming the remedy) or None. `scopes`
    is what the write would leave the target holding."""
    if not target or has_usable_password(target):
        return None
    wanted = [s for s in STEP_UP_SCOPES if s in set(scopes or [])]
    if not wanted:
        return None
    return (f"This account has no usable password, so it cannot hold {wanted}: it "
            f"could never pass the password step-up those scopes require. "
            f"An Owner must set one first.")


# -- The step-up (ruled upstream 2026-09-06, applied fleet-wide 2026-09-09) --
# "Ask for the password again when creating accounts or changing roles" - and
# at every other door to durable authority: permission writes that add
# manage_users/manage_system, and the peer registry. The threat is a stolen
# session on an unlocked device: a bearer token proves possession of a
# browser, the password proves the person. The design was attacked under
# three lenses (completeness of the gate, accounts that cannot satisfy it,
# blast radius) before it shipped; the contract below is what survived.
#
# Contract, fixed by the attack pass: 400 with a STRING detail on a missing or
# wrong password - never 401 (a client that evicts on 401 would log the
# operator out for a typo) and never 422 (a required field's list-shaped
# detail unmounts the admin panel, which renders `detail` raw). Callers keep
# the field optional (`current_password: str = ""`) and let this raise.
#
# Failures drive the SAME failed_attempts / locked_until the login path uses,
# so the step-up is not a second, unthrottled oracle for the password held
# behind the first one. A lock blocks further step-ups (and login) for the
# lockout window; the session itself stays usable, the way it always has.

def _lockout_check(user: dict) -> None:
    from app.users import unlock_user
    locked_until = user.get("locked_until")
    if not locked_until:
        return
    until = datetime.fromisoformat(locked_until)
    now = datetime.now(timezone.utc)
    if until > now:
        remaining = int((until - now).total_seconds() // 60) + 1
        raise HTTPException(status_code=429,
                            detail=f"Account locked. Try again in {remaining} minute(s).")
    unlock_user(user["id"])


def _count_step_up_failure(user: dict, action: str, how: str) -> None:
    """A wrong password: count it against the account, lock at the login
    threshold. Raises the 429 itself when the lock lands."""
    from app.logger import log
    from app.users import increment_failed_attempts, lock_user
    attempts = increment_failed_attempts(user["id"])
    log("auth_step_up_failed", user_id=user["id"], action=action, how=how,
        attempts=attempts)
    if attempts >= MAX_LOGIN_ATTEMPTS:
        until = (datetime.now(timezone.utc)
                 + timedelta(minutes=LOCKOUT_DURATION_MINUTES)).isoformat()
        lock_user(user["id"], until)
        log("auth_lockout", user_id=user["id"], username=user.get("username"),
            stage="step_up")
        raise HTTPException(status_code=429,
                            detail=f"Too many failed attempts. Account locked for "
                                   f"{LOCKOUT_DURATION_MINUTES} minutes.")


def require_step_up(current_user: dict, current_password: str, action: str) -> None:
    """The caller's OWN password, verified against their stored hash, or 400.

    `action` is a verb phrase ("create an account") - it lands in the refusal
    the operator reads and in the log line."""
    from app.logger import log
    from app.users import reset_failed_attempts
    if not has_usable_password(current_user):
        raise HTTPException(
            status_code=400,
            detail=f"This account has no usable password, so it cannot {action}. "
                   f"An Owner must set one first.")
    _lockout_check(current_user)
    if not (current_password or "").strip():
        raise HTTPException(status_code=400,
                            detail=f"Your current password is required to {action}")
    if not verify_password(current_password, current_user["password_hash"]):
        _count_step_up_failure(current_user, action, "password")
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    reset_failed_attempts(current_user["id"])
    log("auth_step_up", user_id=current_user["id"], action=action, how="password")


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


# The cost equalizer for authenticate_user's missing-user branch (see there).
#
# Computed at import THROUGH hash_password rather than pasted in as a literal,
# and that IS the point: bcrypt encodes its work factor in the hash, and a
# dummy only equalizes if its factor matches the stored ones. hash_password
# owns that factor (bcrypt.gensalt()), so routing through it keeps the two
# aligned by construction on the day that default moves or someone passes
# explicit rounds.
#
# A committed literal would be SAFE - it is the hash of a throwaway string, no
# account carries it as a password_hash, and a bcrypt hash of an unknown input
# gives an attacker nothing - so safety is not why it was rejected. DRIFT is: a
# literal freezes the work factor at whatever the library defaulted to on the
# day it was pasted. Real hashes then get more expensive, the dummy stays
# cheap, and the timing gap reopens in the exact direction this fix closes,
# with nothing failing to say so.
#
# The input is random bytes, so no plaintext exists anywhere that verifies
# against it - this can never quietly become a usable credential.
#
# Eager, not lazy. Import costs one bcrypt (about 0.4 s, once per process,
# alongside DB schema init and Chroma boot). A lazily-built dummy would save
# that, at the price of a first-miss that pays hash PLUS verify and a window
# in which the equalizer does not exist yet - a control that installs itself
# on first use is a control that is absent exactly once, and "absent exactly
# once" is how a probe gets its baseline.
_DUMMY_PASSWORD_HASH = hash_password(os.urandom(32).hex())


def authenticate_user(username: str, password: str) -> dict | None:
    """Verify a username/password pair. None on any failure.

    The missing-user branch runs a bcrypt verify it KNOWS will fail, against a
    fixed dummy hash. Without it this function returned in about 1 ms for an
    unknown username and about 383 ms for a known one (measured in-container on
    the reference surface 2026-09-06, n=20, medians) - a 382 ms signal readable from ONE
    request, so the login throttle's budget of 10 attempts per 5 minutes per IP
    bought an attacker roughly 2,880 usernames a day. A crawl, not an
    impossibility. Fleet-ported 2026-09-10 together with that throttle.

    get_user_by_username also filters is_active, so the fast path additionally
    named every DEACTIVATED account.

    A constant-time comparison would not have helped: the branch that leaks is
    the one that never reaches a comparison at all. Paying the hash is the only
    way to make the two branches cost the same thing.

    Honest scope: this makes the two branches cost the SAME WORK, not the same
    number of nanoseconds. bcrypt varies with load and the caller does one
    extra DB write on a known-user failure, so the residual delta is small and
    noisy rather than zero. The claim is that existence is no longer readable
    from a single request.
    """
    user = get_user_by_username(username)
    if not user:
        verify_password(password, _DUMMY_PASSWORD_HASH)
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
