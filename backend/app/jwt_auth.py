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
from typing import Annotated

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

# The per-account lockout knob. ONE env read for its readers - login,
# mfa/complete (both in routers/auth.py, which re-exports these names), the
# step-up below, and the count of passwords checked at the step-up doors
# (security.count_password_attempt, which reads these two names from this
# module when it is called) - because two lockout rules on one account is how
# one of them ends up being the weaker one nobody remembers. Tests monkeypatch
# routers/auth.py's binding for the login path.
MAX_LOGIN_ATTEMPTS       = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_DURATION_MINUTES = int(os.getenv("LOCKOUT_DURATION_MINUTES", "15"))

# -- Unusable passwords (the sentinel, ported from upstream 2026-09-09) -------
# Upstream's SSO-provisioned accounts have no password anyone knows; they used
# to carry bcrypt(random), unguessable but indistinguishable from a real hash,
# so no rule could be ENFORCED on them. The sentinel is not a bcrypt string
# at all (Django's "!" pattern): verify_password refuses it - after paying
# the dummy bcrypt round, since 2026-09-21, so the refusal is not a timing
# tell - has_usable_password can see it, and the role/permission writers
# refuse to hand such an account manage_users or manage_system. This surface
# has no SSO today; the rule rides here so a port inherits it.
UNUSABLE_PASSWORD_PREFIX = "!"

# What an account with no usable password is told about getting one. This
# surface has no route that sets a password for such an account - the
# password change takes the current one, which is the thing it lacks - so the
# sentence says that and names no remedy that does not exist. A port that
# creates such accounts (single sign-on) and adds the route that sets a
# password for one changes this sentence to name it.
NO_PASSWORD_REMEDY = "Nothing on this instance sets a password for an account that has none."


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
    # and turn that account's login into a 500. Refuse it plainly - but PAY
    # THE ROUND FIRST (ruled 2026-09-21): a sub-millisecond refusal for a
    # sign-in-only account against a full bcrypt round for every other
    # username told a caller which accounts have no password - the same
    # oracle the login equalizer below closes for unknown usernames.
    #
    # Text that cannot be encoded (JSON can carry half of a surrogate pair)
    # is a wrong password, not an error: it can never match a stored one,
    # since hash_password encodes the same way. The round is paid for it too,
    # so the answer costs what any wrong password costs, and the caller
    # counts the miss. Raising here made the sign-in answer a server error
    # with no miss counted.
    try:
        raw = plain.encode()
    except UnicodeEncodeError:
        bcrypt.checkpw(b"", _DUMMY_PASSWORD_HASH.encode())
        return False
    if not has_usable_password(hashed):
        bcrypt.checkpw(raw, _DUMMY_PASSWORD_HASH.encode())
        return False
    return bcrypt.checkpw(raw, hashed.encode())


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
            f"{NO_PASSWORD_REMEDY}")


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
#
# The lock is not the bound on its own. It is looked at before the hash and
# counted after it, and an unlock clears it on a proof that is not this
# password (an administrator's own password, or the account's authenticator
# code - see the unlock route). So the
# password helper also counts each password in security.count_password_attempt
# BEFORE it is hashed: however many requests arrive together, no more than
# MAX_LOGIN_ATTEMPTS passwords are checked for one account per lockout window,
# and only a right password ends that count early - an unlock does not. The
# lock itself is read from the stored row, not from the copy the session's
# dependency loaded before the handler ran, and is looked at once more in the
# statement that clears the count after a right password.

def _stored_row(user: dict) -> dict:
    """The account's row as the database holds it NOW. The dict a route hands
    the step-up was read by the session's dependency before the handler ran,
    so a lock or a password stored since is not in it."""
    row = get_user_by_id(user["id"])
    if row is None:
        # Deactivated since the session was read: the answer the session's
        # own dependency gives on the next request.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return row


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


def _lock_minutes(user: dict) -> int:
    """The minutes left on the lock `user` carries, as _lockout_check words
    them, or 0 when no lock stands. Reads the dict it is handed and clears
    nothing."""
    locked_until = user.get("locked_until")
    if not locked_until:
        return 0
    left = (datetime.fromisoformat(locked_until) - datetime.now(timezone.utc)).total_seconds()
    return int(left // 60) + 1 if left > 0 else 0


def lock_minutes_left(user: dict) -> int:
    """The same, read from the account's row as the database holds it now."""
    return _lock_minutes(_stored_row(user))


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
    the operator reads and in the log line. The refusals address the caller
    ("you cannot ..."), so they read right for every phrase a door passes in,
    "change your password" included.

    The order is the control. The two refusals that read no password come
    first (no usable password, an empty one), with the lock between them, read
    from the stored row. Then the attempt is COUNTED, in one write, and only
    then is the password hashed: past the limit the answer is 429 and nothing
    is hashed, for a right password as for a wrong one. A right password
    clears that count and the lock's count; an unlock clears the lock's count
    alone."""
    from app.logger import log
    from app.security import clear_password_attempts, count_password_attempt
    from app.users import clear_failed_attempts_unless_locked, reset_failed_attempts
    if not has_usable_password(current_user):
        raise HTTPException(
            status_code=400,
            detail=f"Your account has no usable password, so you cannot {action}. "
                   f"{NO_PASSWORD_REMEDY}")
    stored = _stored_row(current_user)
    _lockout_check(stored)
    if not (current_password or "").strip():
        raise HTTPException(status_code=400,
                            detail=f"Your current password is required to {action}")
    count_password_attempt(stored["id"])
    # Text that cannot be encoded can never match a stored password -
    # hash_password encodes the same way - so it is a miss like any other:
    # counted against the lock, logged, answered 400, and never handed to
    # the hash (verify_password would answer False for it too, at the price
    # of a round).
    try:
        current_password.encode()
        encodable = True
    except UnicodeEncodeError:
        encodable = False
    if not encodable or not verify_password(current_password, stored["password_hash"]):
        _count_step_up_failure(stored, action, "password")
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if not clear_failed_attempts_unless_locked(stored["id"]):
        # A lock was stored while this password was being checked. The lock
        # refuses a right password too, so this one is refused, with the
        # lock's own answer; the count of passwords checked stays.
        _lockout_check(_stored_row(stored))
        reset_failed_attempts(stored["id"])   # reached only if that lock has already ended
    clear_password_attempts(stored["id"])
    log("auth_step_up", user_id=stored["id"], action=action, how="password")


def require_totp_step_up(current_user: dict, code: str, action: str) -> None:
    """The caller's CURRENT authenticator code, or 400. Two doors take it:
    the re-key (2026-10-02 - after the password, so a session plus the
    password may no longer swap in a new authenticator without the old
    one), and, on a surface that has the door, resetting your own password
    (the "forgot it but still signed in" recovery survives because a token
    thief does not hold the authenticator). An account with no second
    factor cannot take this door and is told the other one.

    Every code checked here counts in security's per-account code bound
    before it is read (a right one clears it) - the bound a correct password
    cannot clear (on the re-key, the password step-up just before this one
    has cleared the lock's counter). A wrong one also counts against the
    account lock, as every step-up failure does."""
    if not current_user.get("mfa_enabled"):
        raise HTTPException(
            status_code=400,
            detail=f"To {action} you need two-factor enrolled on your account and a "
                   f"current authenticator code. Enroll first, or use "
                   f"PATCH /api/auth/me/password with your current password.")
    _check_current_code(current_user, code, action)


def require_code_to_unlock_yourself(current_user: dict, code: str, who_else: str) -> None:
    """Unlocking your OWN account: a current authenticator code, or 400.

    An unlock clears the sign-in's lock and its count of misses. A session
    that could do that for its own account on the session alone could hand
    itself back, as often as it liked, the guesses the lock had stopped. So
    the caller's own account takes the one proof a session does not carry:
    the code. (Refusing the caller's own id outright would strand the one
    account nobody else may unlock - see the unlock route.)

    The code is checked as require_totp_step_up checks one - counted in
    security's per-account code bound before it is read, cleared there by a
    right one - with two differences, both because the lock is the thing a
    right code clears here. The lock does not refuse this door. And a wrong
    code while the lock stands is not counted against the lock: it already
    stands, and the sign-in does not count a miss against a standing lock
    either. With no lock standing a wrong code counts toward it, as every
    step-up failure does.

    An account with no authenticator enrolled has no proof to give here. It
    is told to wait, with the minutes left, or to ask `who_else` - the route's
    words for who else may unlock an account of this tier.

    ONCE PER LOCKOUT WINDOW. A right code here clears the lock and its
    count of misses, so without a count of its own this door would hand the
    sign-in's guesses back as often as it was asked: whoever holds a live
    session and the authenticator, and not the password, could unlock, guess
    until the lock landed, and unlock again. security.count_own_unlock takes
    the window's one place BEFORE the code is read and refuses with 429 when
    it is taken, a right code included; the place is handed back when no
    unlock followed (the code was missing, wrong, or refused by the code
    bound), so a mistyped code does not cost the account its remedy. Nothing
    else ends that count before its window does. A release gives back only
    the place its own request took, in the window it took it in: one
    that arrives after that window has ended changes nothing, so a request
    held across a window's edge cannot free a place in the next one.

    Accepted, on purpose: under a flood of blank or wrong requests from the
    same session, a right code can be answered 429 while another request
    holds the window's one place for a moment. That fails toward no unlock,
    and the caller retries."""
    from app.security import count_own_unlock, release_own_unlock
    if not current_user.get("mfa_enabled"):
        minutes = lock_minutes_left(current_user)
        what_now = (f"Wait {minutes} minute(s) for the lock to end, or ask {who_else} "
                    f"to unlock it." if minutes else "Your account is not locked now.")
        raise HTTPException(
            status_code=400,
            detail=f"Unlocking your own account takes a current authenticator code, "
                   f"and your account has no authenticator enrolled. {what_now}")
    window = count_own_unlock(current_user["id"], who_else)
    try:
        _check_current_code(current_user, code, "unlock your own account",
                            lock_refuses=False)
    except HTTPException:
        release_own_unlock(current_user["id"], window)
        raise


def _check_current_code(current_user: dict, code: str, action: str, *,
                        lock_refuses: bool = True) -> None:
    """What every door that takes the caller's current code shares, for an
    account that has a factor enrolled. `lock_refuses` is False at one door
    only, the unlock of the caller's own account."""
    import pyotp  # function-local, like every other pyotp use in this app
    from app.logger import log
    from app.security import count_totp_attempt, clear_totp_attempts
    from app.users import reset_failed_attempts
    if current_user.get("mfa_secret_unreadable") or not current_user.get("mfa_secret"):
        # Truthful refusal, not pyotp.TOTP(None) -> 500: the seed exists and
        # cannot be read (a restore under a rotated JWT_SECRET_KEY - T9).
        raise HTTPException(
            status_code=400,
            detail="Two-factor is enabled on this account but its stored seed cannot "
                   "be read with this instance's JWT_SECRET_KEY, so the code cannot be "
                   "checked. Re-key the seed column (rekey_at_rest.py) first.")
    # The stored row, as at the password door: a lock stored since the
    # session's dependency read the account is seen here too. A lock that
    # stands refuses, except at the door that clears it; one that has ended
    # is cleared at every door, so a miss after it counts from one.
    stored = _stored_row(current_user)
    lock_stands = _lock_minutes(stored) > 0
    if lock_refuses or not lock_stands:
        _lockout_check(stored)
    if not (code or "").strip():
        raise HTTPException(status_code=400,
                            detail=f"A current authenticator code is required to {action}")
    count_totp_attempt(current_user["id"])
    # Text that cannot be encoded can match no code, and the comparison would
    # raise on it: a server error, counted in the code bound and nowhere
    # else. It is a wrong code like any other.
    if not can_be_encoded(code) or not pyotp.TOTP(
            current_user["mfa_secret"]).verify(code.strip(), valid_window=1):
        if lock_stands:
            # Reached only where the lock does not refuse. The code was
            # counted above; the lock is left exactly as it stands.
            log("auth_step_up_failed", user_id=current_user["id"], action=action,
                how="totp", attempts=stored.get("failed_attempts"))
        else:
            _count_step_up_failure(current_user, action, "totp")
        raise HTTPException(status_code=400, detail="Invalid authenticator code")
    clear_totp_attempts(current_user["id"])
    reset_failed_attempts(current_user["id"])
    log("auth_step_up", user_id=current_user["id"], action=action, how="totp")


# -- T9: the stranded second factor (upstream 2026-09-06, fleet port ----------
# 2026-09-10). The MFA seed is Fernet-encrypted under a key derived from
# JWT_SECRET_KEY (crypto_at_rest). A database restored under a DIFFERENT
# secret, a rotation without the re-key sweep, or one tampered row leaves the
# seed unreadable, and users._user_to_dict reports that as the PAIR
# mfa_secret=None + mfa_secret_unreadable=True instead of raising (which was
# HTTP 500 on every read of that account). The pair must FAIL CLOSED at every
# door that mints or admits a session: reading "unreadable" as "never
# enrolled" would mean anyone able to write garbage into one column has
# stripped the second factor. Reads that do not mint stay tolerant, and
# /api/auth/me REPORTS the state so a client can render the truth.
#
# One helper, not an inline block per door, so the fleet drift checker can pin
# the CALLS by substring - a wrong body inside an existing function is what a
# name diff cannot see. Three functions because a surface with machine doors
# (an OAuth-backed MCP endpoint) answers RFC 6749 bodies or None there, never
# an HTTPException.

MFA_SEED_UNREADABLE_DETAIL = (
    "Two-factor is enabled on this account but its stored seed cannot be "
    "decrypted with this instance's JWT_SECRET_KEY. The database was "
    "restored under a different secret, or the secret was rotated without "
    "re-keying. Sign-in is refused rather than downgraded to password-only. "
    "Fix: re-key at rest offline (backend/scripts/rekey_at_rest.py - stop the "
    "backend, run it inside the backend container against /app/data/history.db, "
    "it prompts for the previous and current secrets), or have an operator "
    "reset MFA for this account (POST /api/admin/users/<id>/mfa-reset) and "
    "enroll again."
)


def mfa_seed_stranded(user: dict | None) -> bool:
    """True when a second factor IS enrolled and its seed cannot be read.
    Both halves on purpose: an unreadable seed on an account that never
    finished enrolling was never a factor, and mfa/setup overwrites it under
    the current key, so that case self-heals and must not be blocked."""
    return bool(user and user.get("mfa_enabled") and user.get("mfa_secret_unreadable"))


def mfa_seed_unreadable_detail() -> str:
    """The refusal text. A function so a surface with an offboarding freeze
    can name the one remedy the freeze still allows."""
    return MFA_SEED_UNREADABLE_DETAIL


def record_mfa_seed_stranded(user: dict, stage: str, *, revoke_sessions: bool = False) -> None:
    """The durable side of a refusal: the log line, and - when the presented
    credential is one that OUTLIVES the rotation (a refresh token) - the whole
    session family revoked, so the stale credential cannot simply be retried.
    This surface has no security_events table; the log line is the record."""
    from app.logger import log
    fields = {"user_id": user["id"], "username": user.get("username"), "stage": stage}
    if revoke_sessions:
        from app.users import revoke_all_user_tokens
        revoke_all_user_tokens(user["id"])
        fields["action"] = "family_revoked"
    log("auth_mfa_seed_unreadable", **fields)


def refuse_if_mfa_seed_stranded(user: dict | None, stage: str, *, revoke_sessions: bool = False) -> None:
    """403 with the remedy at every door that mints a session for a stranded
    account; a no-op for everyone else. 403 and not 401: the password was
    correct, and a 401 would send the human back to retype it forever. No
    enumeration value either - every caller reaches this after the presented
    credential already verified."""
    if not mfa_seed_stranded(user):
        return
    record_mfa_seed_stranded(user, stage, revoke_sessions=revoke_sessions)
    raise HTTPException(status_code=403, detail=mfa_seed_unreadable_detail())


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
    # Bounded before the decode, like the middleware (app/auth.py,
    # MAX_CREDENTIAL_CHARS): the route guards and the chat route hand this the
    # header's token as it arrived.
    from app.auth import MAX_CREDENTIAL_CHARS
    if len(token) > MAX_CREDENTIAL_CHARS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
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
    """Return a list of policy violations. Empty list = valid.

    Every route that sets a password asks this before it hashes one, so the
    one thing hash_password cannot take is refused here, in words: text that
    cannot be encoded (JSON can carry half of a surrogate pair). It is the
    only violation reported for such text - the others would be noise."""
    try:
        password.encode()
    except UnicodeEncodeError:
        return ["Password contains a character that cannot be stored. "
                "Choose a different password"]
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if REQUIRE_UPPERCASE and not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter")
    if REQUIRE_SPECIAL_CHARS and not any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?" for c in password):
        errors.append("Password must contain at least one special character")
    return errors


def can_be_encoded(text: str) -> bool:
    """False for text that is not valid Unicode - JSON can carry half of a
    surrogate pair. Such text can match no stored name, password or code,
    and encoding it raises; every door that takes text from a request body
    and compares or stores it asks this first."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def username_refusal(username: str) -> str | None:
    """Why this text cannot be an account's name, or None. One reason today:
    text that cannot be encoded cannot be stored, and the write would raise.
    Every route that creates an account or renames one asks this before it
    writes."""
    return stored_text_refusal("Username", username)


def stored_text_refusal(what: str, text) -> str | None:
    """Why this text cannot be stored on an account, or None. `what`
    names the field, capitalised, for the caller who reads the refusal. One
    reason today: text that cannot be encoded cannot be stored, and the
    write would raise. Every route that stores text from a request body on
    an account - its name, its department, whatever else a surface keeps -
    asks this before it writes. Anything that is not text is not this
    function's to judge."""
    if isinstance(text, str) and not can_be_encoded(text):
        return (f"{what} contains a character that cannot be stored. "
                f"Choose a different {what.lower()}")
    return None


# -- A user id from a route's path --------------------------------------------
# The users table keeps ids in a 64-bit integer, and a route's path takes any
# whole number: one past that range reached the database and raised there, a
# server error. Such a number names no account, so it is handed on as 0,
# which names none either - and every route with {user_id} in its path then
# answers for it exactly what it answers for an id that names no account,
# in the same order (at the routes that ask for a proof first, after the
# proof). One guard for all of them: they take their id as UserId.
_LARGEST_STORED_ID = 2 ** 63 - 1


def stored_user_id(user_id: int) -> int:
    """The id from the path when the users table can hold it, else 0 - an id
    no account has. Never raises, so it changes no route's order."""
    return user_id if 0 < user_id <= _LARGEST_STORED_ID else 0


UserId = Annotated[int, Depends(stored_user_id)]


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


def user_from_token(token: str) -> dict:
    """The active account a session token names, or the 401 that says why
    not. get_current_user's decision, callable where no dependency runs."""
    payload = decode_access_token(token)
    user_id = int(payload.get("sub", 0))
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_user(credentials=Depends(oauth2_scheme)) -> dict:
    """The enforcing dependency: raises 401 on missing or invalid
    credentials. Used route-level on every non-public route (a wiring test
    sweeps app.routes to keep that true), so authorization holds even if the
    middleware layer is ever disabled."""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user_from_token(credentials.credentials)


async def require_owner(current_user: dict = Depends(get_current_user)) -> dict:
    """Owner-only. Guards system/ops endpoints - model settings, backups,
    eval internals - that admins must not reach."""
    if not is_owner(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required")
    return current_user


def check_permission(user: dict, scope: str) -> dict:
    """The Owner (full bypass) or an account whose resolved permissions
    include the scope; anyone else, 403. require_permission's decision."""
    if is_owner(user):
        return user
    if scope not in effective_permissions(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission required: {scope}",
        )
    return user


def require_permission(scope: str):
    """Dependency factory: passes for the Owner (full bypass) or any user
    whose resolved permissions include the scope."""
    async def _check(current_user: dict = Depends(get_current_user)) -> dict:
        return check_permission(current_user, scope)
    return _check


def require_any_permission(*scopes: str):
    """Dependency factory for a READ that holders of more than one scope
    already reach through their writes: passes for the Owner (full bypass)
    or any user whose resolved permissions include at least one of the
    scopes; anyone else, 403 (2026-10-30). A factory of its own rather than
    a second argument to require_permission, so the route pin in
    tests/test_route_authz_wiring.py names every scope a route admits."""
    async def _check_any(current_user: dict = Depends(get_current_user)) -> dict:
        if is_owner(current_user):
            return current_user
        held = effective_permissions(current_user)
        if not any(scope in held for scope in scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {' or '.join(scopes)}",
            )
        return current_user
    return _check_any


def permission_guard(scope: str):
    """require_permission(scope)'s question, asked of a raw Authorization
    header: the account, or the route's own 401 / 403 raised."""
    def guard(authorization: str) -> dict:
        return check_permission(
            user_from_token(authorization.removeprefix("Bearer ").strip()), scope)
    return guard


def door_guard(guards: dict):
    """The body ceiling's check for its document doors (app/body_limit.py).
    `guards` names, for each door's path, its route's guard as a function of
    the Authorization header - permission_guard(scope) for a route that asks
    require_permission(scope). The check is asked before a byte of the body
    is read, and asks what the route will: a credential first (no credential,
    no account lookup), then the door's guard, whose refusal - the route's
    own 401 or 403 - is raised through, and whose answer is the account it
    admitted: a guard that returns nothing admits nobody, and so does a door
    with no guard named here."""
    def check(authorization: str, path: str) -> bool:
        from app.auth import presents_a_credential
        guard = guards.get(path)
        if guard is None or not presents_a_credential(authorization):
            return False
        return bool(guard(authorization))
    return check
