"""Authentication: login, MFA, tokens, sessions, self-service, and the claim.

Fourth router out of main.py, moved early on purpose: it has no shared-module
dependencies at all, so it is the cheapest of the ten to get wrong-free while
the diff is still small.

NAMING: app/auth.py is a DIFFERENT module - AuthMiddleware, EXCLUDED_PATHS, and
the boot guard that refuses to start on a default JWT secret. This file is
app/routers/auth.py and imports nothing from it. main registers it as
`auth as auth_router` so the two never read alike at a call site.

NO prefix= and NO router-level dependencies=[]. Six of these paths are literal
entries in app/auth.py EXCLUDED_PATHS (/api/auth/login, /refresh, /setup,
/needs-setup, /config, /mfa/complete) and the middleware matches the exact
string - a prefix would silently un-exclude them, and production would start
401-ing unauthenticated login while the ENABLE_AUTH=false suite stayed green.
A router-level dependency would be worse: it would protect the six bootstrap
routes, so nobody could log in on a fresh deployment at all.

The three constants below have exactly one reader - this router - so they live
here rather than in runtime_config, which is for names main AND routers share.
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.jwt_auth import (authenticate_user, create_access_token,
                          create_refresh_token, hash_token, hash_password,
                          verify_password, get_current_user, validate_password,
                          create_mfa_challenge_token, decode_mfa_challenge_token)
from app.logger import log
from app.metrics import increment
from app.permissions import effective_permissions
from app.security import (check_setup_rate_limit, check_mfa_challenge,
                          record_mfa_failure, burn_mfa_challenge,
                          client_ip_from_request, verify_setup_claim_code,
                          burn_setup_claim_code)
from app.users import (create_user, owner_exists, store_refresh_token,
                       get_refresh_token, revoke_refresh_token,
                       revoke_all_user_tokens, get_user_by_id, set_mfa_secret,
                       enable_mfa, increment_failed_attempts,
                       reset_failed_attempts, lock_user, unlock_user,
                       list_user_sessions, revoke_refresh_token_by_id,
                       update_user_password, update_user_username)

router = APIRouter()

REQUIRE_MFA              = os.getenv("REQUIRE_MFA", "false").lower() == "true"
MAX_LOGIN_ATTEMPTS       = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_DURATION_MINUTES = int(os.getenv("LOCKOUT_DURATION_MINUTES", "15"))


class LoginRequest(BaseModel):
    username: str
    password: str
class ClaimDeploymentRequest(BaseModel):
    """The first-Owner claim. Its own model, NOT CreateUserRequest.

    The claim code is meaningless on POST /api/users (an authenticated Owner
    creating a colleague), so a shared optional field would document a parameter
    that route silently ignores. And `role` has no business being
    caller-supplied here - the first user is the Owner by definition, and the
    endpoint has always hard-coded that; inheriting a settable `role` on an
    UNAUTHENTICATED endpoint is a field waiting to be wired up wrong.
    """
    username: str
    password: str
    claim_code: str = ""
@router.post("/api/auth/login")
def login(request: LoginRequest):
    from datetime import datetime, timezone, timedelta
    from app.users import get_user_by_username as _get_user

    user = _get_user(request.username)

    # REQUIRE_MFA enforcement: when the host sets it, a password login on an
    # account with NO enrolled TOTP factor is refused outright - enroll from
    # an existing session first, THEN flip the env. Checked after password
    # verification (below) so this cannot become an account-enumeration
    # oracle.
    #
    # Check lockout before verifying password
    if user and user.get("locked_until"):
        locked_until = datetime.fromisoformat(user["locked_until"])
        if locked_until > datetime.now(timezone.utc):
            remaining = int((locked_until - datetime.now(timezone.utc)).total_seconds() // 60) + 1
            raise HTTPException(status_code=429, detail=f"Account locked. Try again in {remaining} minute(s).")
        else:
            unlock_user(user["id"])
            user = _get_user(request.username)

    if not user or not authenticate_user(request.username, request.password):
        increment("auth_failures_total")
        if user:
            attempts = increment_failed_attempts(user["id"])
            if attempts >= MAX_LOGIN_ATTEMPTS:
                until = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_DURATION_MINUTES)).isoformat()
                lock_user(user["id"], until)
                log("auth_lockout", user_id=user["id"], username=request.username)
                raise HTTPException(status_code=429, detail=f"Too many failed attempts. Account locked for {LOCKOUT_DURATION_MINUTES} minutes.")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # The REQUIRE_MFA refusal (see the comment block above the lockout check).
    if REQUIRE_MFA and not user.get("mfa_enabled"):
        log("auth_mfa_required_refusal", user_id=user["id"], username=user["username"])
        raise HTTPException(
            status_code=403,
            detail="MFA is required on this instance and this account has no "
                   "TOTP enrolled. Enroll from an existing session, then retry.")

    # MFA check. Deliberately NO reset_failed_attempts before this branch: the
    # password is only half of an MFA login, and clearing the counter when the
    # challenge was minted let a password-holding attacker zero the account
    # lock with every re-login, so MFA failures could never accumulate (fixed
    # 2026-08-27; the re-login test in test_mfa_challenge_guard.py pins it).
    # The counter clears in mfa_complete, on full success.
    if user.get("mfa_enabled"):
        mfa_token = create_mfa_challenge_token(user["id"])
        log("auth_mfa_challenge", user_id=user["id"], username=user["username"])
        return {"mfa_required": True, "mfa_token": mfa_token}

    reset_failed_attempts(user["id"])
    access_token = create_access_token(user["id"], user["username"], user["role"])
    raw_refresh, expires_at = create_refresh_token(user["id"])
    store_refresh_token(user["id"], hash_token(raw_refresh), expires_at)
    log("auth_login", user_id=user["id"], username=user["username"])
    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
    }


class MFACompleteRequest(BaseModel):
    mfa_token: str
    code: str


@router.post("/api/auth/mfa/complete")
def mfa_complete(request: MFACompleteRequest):
    """Exchange MFA challenge token + TOTP code for full access/refresh tokens.

    HARDENED 2026-08-27. This endpoint had no attempt counter, no lockout, and
    no invalidation of the challenge after use - while /api/auth/login has all
    three. A valid mfa_token was therefore an unbounded guessing permit for its
    full lifetime against a 6-digit code, with valid_window=1 making three codes
    acceptable at any instant, and it stayed replayable after a successful login.

    Three bounds now, in the order they can fail:
      1. SINGLE USE - the challenge carries a jti and is burned on success.
      2. PER CHALLENGE - a small number of wrong codes kills that challenge.
      3. PER ACCOUNT - failures also drive the SAME failed_attempts/locked_until
         the password path uses, so grinding fresh challenges walks into the
         account lock rather than resetting a per-challenge counter each time.
         The lockout check below is a copy of login's and not a new policy: two
         different lockout rules on one account is how one of them ends up being
         the weaker one nobody remembers.
    """
    from datetime import datetime, timezone, timedelta
    import pyotp
    user_id, jti = decode_mfa_challenge_token(request.mfa_token)
    check_mfa_challenge(jti)
    user = get_user_by_id(user_id)
    if not user or not user.get("mfa_enabled") or not user.get("mfa_secret"):
        raise HTTPException(status_code=400, detail="MFA not configured for this account")

    # Mirrors /api/auth/login - this endpoint used to bypass lockout entirely, so
    # a locked account holding a live challenge could still walk in.
    if user.get("locked_until"):
        locked_until = datetime.fromisoformat(user["locked_until"])
        if locked_until > datetime.now(timezone.utc):
            remaining = int((locked_until - datetime.now(timezone.utc)).total_seconds() // 60) + 1
            raise HTTPException(status_code=429, detail=f"Account locked. Try again in {remaining} minute(s).")
        unlock_user(user["id"])

    totp = pyotp.TOTP(user["mfa_secret"])
    if not totp.verify(request.code, valid_window=1):
        record_mfa_failure(jti)
        increment("auth_failures_total")
        attempts = increment_failed_attempts(user["id"])
        if attempts >= MAX_LOGIN_ATTEMPTS:
            until = (datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_DURATION_MINUTES)).isoformat()
            lock_user(user["id"], until)
            log("auth_lockout", user_id=user["id"], username=user["username"], stage="mfa")
            raise HTTPException(status_code=429, detail=f"Too many failed attempts. Account locked for {LOCKOUT_DURATION_MINUTES} minutes.")
        raise HTTPException(status_code=401, detail="Invalid authenticator code")

    burn_mfa_challenge(jti)
    reset_failed_attempts(user["id"])
    access_token = create_access_token(user["id"], user["username"], user["role"])
    raw_refresh, expires_at = create_refresh_token(user["id"])
    store_refresh_token(user["id"], hash_token(raw_refresh), expires_at)
    log("auth_mfa_complete", user_id=user["id"])
    return {
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
    }


class MFASetupRequest(BaseModel):
    rekey: bool = False


@router.post("/api/auth/mfa/setup")
def mfa_setup(request: MFASetupRequest | None = None,
              current_user: dict = Depends(get_current_user)):
    """Generate a new TOTP secret and return the provisioning URI + QR code
    PNG (base64).

    Re-key guard (2026-09-05, readiness-audit port): calling this while MFA
    is ENABLED replaces the secret and flips mfa_enabled off - so any holder
    of a live access token could strip an account's second factor with one
    bodyless POST. An enabled account must now say rekey explicitly; on
    refusal the stored secret is untouched.
    """
    import pyotp, qrcode, base64
    from io import BytesIO
    if current_user.get("mfa_enabled"):
        if not (request and request.rekey):
            raise HTTPException(
                status_code=409,
                detail="MFA is already enabled for this account. Pass rekey=true "
                       "to deliberately re-enroll; that replaces the secret and "
                       "disables MFA until the new code verifies.")
        log("auth_mfa_rekey_started", user_id=current_user["id"],
            username=current_user["username"])
    secret = pyotp.random_base32()
    set_mfa_secret(current_user["id"], secret)
    instance_name = os.getenv("VITE_INSTANCE_NAME", "Architecture Zero")
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=current_user["username"],
        issuer_name=instance_name,
    )
    img = qrcode.make(uri)
    buf = BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return {"secret": secret, "uri": uri, "qr": f"data:image/png;base64,{qr_b64}"}


class MFAEnableRequest(BaseModel):
    code: str


@router.post("/api/auth/mfa/enable")
def mfa_enable(request: MFAEnableRequest, current_user: dict = Depends(get_current_user)):
    """Verify TOTP code against pending secret and activate MFA."""
    import pyotp
    user = get_user_by_id(current_user["id"])
    if not user or not user.get("mfa_secret"):
        raise HTTPException(status_code=400, detail="Call /api/auth/mfa/setup first")
    totp = pyotp.TOTP(user["mfa_secret"])
    if not totp.verify(request.code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid authenticator code")
    enable_mfa(current_user["id"])
    log("auth_mfa_enabled", user_id=current_user["id"])
    return {"status": "MFA enabled"}


@router.get("/api/auth/sessions")
def get_sessions(current_user: dict = Depends(get_current_user)):
    """List active sessions (refresh tokens) for the current user."""
    return {"sessions": list_user_sessions(current_user["id"])}


@router.delete("/api/auth/sessions/{token_id}")
def revoke_session(token_id: int, current_user: dict = Depends(get_current_user)):
    """Revoke a specific session by its ID."""
    revoke_refresh_token_by_id(token_id, current_user["id"])
    log("auth_revoke_session", user_id=current_user["id"], token_id=token_id)
    return {"status": "session revoked"}


@router.post("/api/auth/refresh")
def refresh(req: Request):
    # refresh token passed in Authorization header as "Bearer <token>"
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_token = auth_header.removeprefix("Bearer ").strip()
    else:
        raise HTTPException(status_code=401, detail="Refresh token required")

    record = get_refresh_token(hash_token(raw_token))
    if not record:
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token")

    from datetime import datetime, timezone
    if datetime.fromisoformat(record["expires_at"]) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Refresh token expired")

    from app.users import get_user_by_id
    user = get_user_by_id(record["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    revoke_refresh_token(hash_token(raw_token))
    access_token = create_access_token(user["id"], user["username"], user["role"])
    new_raw, expires_at = create_refresh_token(user["id"])
    store_refresh_token(user["id"], hash_token(new_raw), expires_at)
    return {"access_token": access_token, "refresh_token": new_raw, "token_type": "bearer"}


@router.post("/api/auth/logout")
def logout(req: Request, current_user: dict = Depends(get_current_user)):
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        revoke_all_user_tokens(current_user["id"])
    log("auth_logout", user_id=current_user["id"])
    return {"status": "logged out"}


@router.delete("/api/auth/sessions")
def revoke_sessions(current_user: dict = Depends(get_current_user)):
    """Revoke all active refresh tokens for the current user (sign out
    everywhere)."""
    revoke_all_user_tokens(current_user["id"])
    log("auth_revoke_sessions", user_id=current_user["id"])
    return {"status": "all sessions revoked"}


@router.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "role": current_user["role"],
        "department": current_user.get("department", "general"),
        "permissions": effective_permissions(current_user),
        # Enrollment state (2026-09-05, with the re-key guard below): a client
        # that cannot see the truth renders "Set up authenticator" to enrolled
        # accounts, and one tap of that used to silently de-enroll them.
        "mfa_enabled": bool(current_user.get("mfa_enabled")),
    }


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.patch("/api/auth/me/password")
def change_password(request: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    if not verify_password(request.current_password, current_user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    errors = validate_password(request.new_password)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    update_user_password(current_user["id"], hash_password(request.new_password))
    revoke_all_user_tokens(current_user["id"])
    log("auth_password_change", user_id=current_user["id"])
    return {"status": "password updated - please sign in again"}


class ChangeUsernameRequest(BaseModel):
    new_username: str


@router.patch("/api/auth/me/username")
def change_username(request: ChangeUsernameRequest, current_user: dict = Depends(get_current_user)):
    new_username = request.new_username.strip()
    if not new_username or len(new_username) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    if not update_user_username(current_user["id"], new_username):
        raise HTTPException(status_code=409, detail="Username already taken")
    # Re-issue tokens with updated username
    access_token = create_access_token(current_user["id"], new_username, current_user["role"])
    raw_refresh, expires_at = create_refresh_token(current_user["id"])
    revoke_all_user_tokens(current_user["id"])
    store_refresh_token(current_user["id"], hash_token(raw_refresh), expires_at)
    log("auth_username_change", user_id=current_user["id"], new_username=new_username)
    return {
        "status": "username updated",
        "access_token": access_token,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "user": {"id": current_user["id"], "username": new_username, "role": current_user["role"]},
    }


@router.get("/api/auth/needs-setup")
def check_needs_setup():
    return {"needs_setup": not owner_exists()}


@router.get("/api/auth/config")
def auth_config():
    """Public (EXCLUDED_PATHS): what the login screen may offer.

    guest_mode_enabled is the EFFECTIVE value, from the same helper the chat
    gate itself calls - a login screen that computed it separately would
    eventually offer a guest door the server refuses. It discloses nothing new:
    posting to /api/chat already answers the same question, and less politely.
    """
    from app.runtime_config import guest_chat_available
    from app.config import get_config
    return {
        "needs_setup": not owner_exists(),
        "auth_mode": "local",
        "guest_mode_enabled": guest_chat_available(),
        # Guests never reach /api/config, so the chat client had no way to learn
        # this and rendered the retrieval toggle unconditionally - including on
        # instances where the operator had turned it off. The server enforces the
        # setting regardless (see chat.py), so this is about not showing a
        # control that does nothing, not about trust. It discloses no more than
        # the toggle's own behaviour already would.
        "allow_rag_toggle": get_config("allow_rag_toggle", "true") == "true",
    }


@router.post("/api/auth/setup")
def setup_admin(request: ClaimDeploymentRequest, req: Request):
    """One-time endpoint to create the first Owner. Disabled once an Owner
    exists.

    Throttled since 2026-08-27, BEFORE the owner_exists() check so the closed
    path is bounded too.

    CLAIM CODE REQUIRED since 2026-08-27. The throttle bounds attempts and
    cannot close the race this endpoint opens - it is unauthenticated and open
    until an Owner exists, so a deployment that is publicly reachable before its
    operator finishes setup goes to whoever asks first. That is the normal first
    ten minutes of every deployment made from this template, which is why the
    control lands here rather than being left to each operator. The code is
    minted at boot and printed to the container logs; see
    security.setup_claim_code for the shape and its multi-worker caveat.

    ORDER IS DELIBERATE: throttle, then the claimed-ness 403, then the code,
    then the MFA-posture refusal, then the password policy. The code check sits
    above both so an anonymous caller can probe neither the password rules nor
    this deployment's MFA posture without holding the code, and below the 403 so
    the existing claimed-deployment contract is unchanged.
    """
    check_setup_rate_limit(client_ip_from_request(req))
    if owner_exists():
        raise HTTPException(status_code=403, detail="Owner already exists")
    verify_setup_claim_code(request.claim_code)
    # REQUIRE_MFA + an unclaimed deployment is a one-way door, so it is refused
    # here rather than walked into. Claiming does not enroll a factor, and the
    # login below refuses any account without one - while both enrolment routes
    # need a session that login is what grants. So the claim would succeed, burn
    # the code, and leave an Owner who can never sign in, a claim endpoint that
    # now 403s, and no recovery short of editing the database.
    #
    # This was documented rather than enforced ("ORDER MATTERS: enroll accounts
    # first, flip this second"), and prose is the wrong guard for a step whose
    # failure is unrecoverable and whose natural operator instinct - harden the
    # config before first boot - is exactly what triggers it. Refusing costs a
    # restart with one env var changed; the alternative costs the deployment.
    #
    # Deliberately NOT solved by exempting the first Owner from the login
    # refusal: that would mint a session with no factor on an instance whose
    # whole posture says every session has one.
    if REQUIRE_MFA:
        raise HTTPException(
            status_code=409,
            detail="REQUIRE_MFA is set, so this deployment cannot be claimed: "
                   "the first Owner has no way to enroll a factor before their "
                   "first sign-in. Start with REQUIRE_MFA=false, claim the "
                   "deployment, enroll from Settings, then set it to true.")
    errors = validate_password(request.password)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    # users.username is UNIQUE, and a bare IntegrityError from the flush
    # surfaces as a 500 with a SQL traceback - an operator retyping an
    # existing name reads that as "the server is broken", not "pick another
    # name".
    from sqlalchemy.exc import IntegrityError
    try:
        user_id = create_user(request.username, hash_password(request.password), role="owner")
    except IntegrityError:
        raise HTTPException(status_code=409, detail="That username is already taken")
    # Burned only after the user row exists: a failed create means the claim did
    # NOT happen, and retiring the code there would strand the operator with a
    # dead code and no Owner until a container restart.
    burn_setup_claim_code()
    log("auth_setup_owner", user_id=user_id, username=request.username)
    return {"status": "owner created", "user_id": user_id}
