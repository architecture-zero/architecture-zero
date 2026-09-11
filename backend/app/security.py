import re
import time
import os
import ipaddress
import secrets
from fastapi import HTTPException, Request

from app import state_store

# ── Rate limiting ─────────────────────────────────────────────────────────────
ENABLE_RATE_LIMIT    = os.getenv("ENABLE_RATE_LIMIT",    "false").lower() == "true"
RATE_LIMIT_REQUESTS  = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW    = int(os.getenv("RATE_LIMIT_WINDOW",   "60"))   # seconds

# The no-Redis path used to be a dict in this process: every address that ever
# hit the instance kept an entry (bounded later by an amortized sweep), and a
# restart handed every IP a fresh budget. Since 2026-09-11 it is a sliding
# window in the database (app/state_store.py) - restart-proof, shared by every
# worker on this node, expiring with the window it bounds. The Redis path is
# unchanged and still preferred when REDIS_URL is set.


def _check_rate_limit_db(client_ip: str) -> None:
    _, allowed = state_store.bump_window(f"rate:{client_ip}",
                                        RATE_LIMIT_WINDOW, RATE_LIMIT_REQUESTS)
    if not allowed:
        # A refused request does not extend the window - an IP being rejected
        # still expires with its last allowed attempt.
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW}s",
        )


def _check_rate_limit_redis(r, client_ip: str) -> None:
    """Sliding-window rate limit using a Redis sorted set - works across multiple backend instances."""
    now = time.time()
    key = f"az:rl:{client_ip}"
    window_start = now - RATE_LIMIT_WINDOW

    pipe = r.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)         # drop expired entries
    pipe.zadd(key, {f"{now}:{id(object())}": now})      # add current timestamp (unique member)
    pipe.zcard(key)                                      # count entries in window
    pipe.expire(key, RATE_LIMIT_WINDOW + 1)             # auto-expire the key
    results = pipe.execute()

    count = results[2]
    if count > RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW}s",
        )


def client_ip_from_request(request: Request) -> str:
    """Real client IP behind a trusted reverse proxy.

    Behind a reverse proxy the socket peer is always the proxy's address, so
    per-IP rate limiting collapses into ONE shared bucket (useless against
    abuse, and one abuser 429s every real visitor). Deploy the proxy to SET
    X-Real-IP (overwrite, never append) so the header is proxy-owned; it is
    trusted only when the socket peer is private/loopback (i.e. the request
    came through the proxy). A request hitting the published port directly
    is keyed by its socket address and its headers are ignored.
    """
    peer = request.client.host if request.client else "unknown"
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        try:
            addr = ipaddress.ip_address(peer)
            if addr.is_private or addr.is_loopback:
                return real
        except ValueError:
            pass
    return peer


def check_rate_limit(client_ip: str) -> None:
    if not ENABLE_RATE_LIMIT:
        return
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        _check_rate_limit_redis(r, client_ip)
    else:
        _check_rate_limit_db(client_ip)

# -- First-owner claim throttle (2026-08-27) ----------------------------------
# Deliberately NOT routed through check_rate_limit() above, and deliberately not
# gated on ENABLE_RATE_LIMIT: that flag defaults to "false", so a claim endpoint
# "protected" by it would read as guarded in the source and be entirely absent in
# a default deployment. This one has no off switch.
#
# Until 2026-08-27, check_rate_limit was wired to exactly ONE route in this
# application (/api/chat). POST /api/auth/setup - unauthenticated, and the
# endpoint that hands out ownership of the instance - had no throttle at all.
SETUP_MAX_ATTEMPTS = int(os.getenv("SETUP_MAX_ATTEMPTS", "5"))
SETUP_WINDOW       = int(os.getenv("SETUP_WINDOW",       "900"))  # seconds


# -- Per-IP auth throttle (fleet port 2026-09-10, from the reference surface) ----------------
# check_rate_limit() above guards /api/chat only, so /api/auth/login,
# /mfa/complete and /refresh had no per-IP bound at all - the only brakes
# were per-ACCOUNT (failed_attempts/locked_until), which a sprayer sidesteps
# by walking usernames, and the per-challenge MFA counter, which re-arms
# with every fresh challenge. Always on, like check_setup_rate_limit and for
# the same reason: an auth boundary guarded by a flag that defaults off is
# guarded in source and absent in the deployment.
#
# Ported in the SAME commit as the login timing equalizer (jwt_auth.py), on
# purpose: the equalizer makes an unknown username cost a full bcrypt round,
# so an unthrottled login route would turn a timing oracle into a CPU
# amplifier - roughly forty concurrent garbage-username POSTs stall every
# sync endpoint behind the single worker. Neither ships without the other.
#
# Scopes are separate buckets per IP so one exhausted lane cannot starve
# another (a lockout-probing attacker on a shared IP must not eat the
# refresh budget of the legit devices behind the same NAT). Bounds are
# generous for humans and hostile to iteration:
#   login/mfa - AUTH_MAX_ATTEMPTS per AUTH_WINDOW (default 10 per 5 min;
#     the per-account lock still fires first for a real username).
#   refresh   - REFRESH_MAX_ATTEMPTS per AUTH_WINDOW (default 60 per 5 min;
#     refresh is ROUTINE client traffic, so its budget is wide; the token
#     space is 256-bit - this bound is about noise and DB pressure, not
#     guessability).
#
# The setup/claim lane keeps its own dedicated throttle (check_setup_rate_limit
# below). Both windows live in the database since 2026-09-11 (app/state_store.py):
# restart-proof, shared by every worker on this node, expiring with the window
# they bound - the in-process dicts they replaced forgot every count on a redeploy.
AUTH_MAX_ATTEMPTS    = int(os.getenv("AUTH_MAX_ATTEMPTS",    "10"))
REFRESH_MAX_ATTEMPTS = int(os.getenv("REFRESH_MAX_ATTEMPTS", "60"))
AUTH_WINDOW          = int(os.getenv("AUTH_WINDOW",          "300"))  # seconds

def check_auth_rate_limit(client_ip: str, scope: str = "login") -> None:
    """Bound anonymous auth attempts per IP. Always on. Raises 429 at the cap.

    Counts ATTEMPTS, not failures - success does not refund the bucket, which
    keeps the check before any credential work (no oracle about whether the
    attempt would have succeeded) and the accounting one-line simple.
    """
    limit = REFRESH_MAX_ATTEMPTS if scope == "refresh" else AUTH_MAX_ATTEMPTS
    _, allowed = state_store.bump_window(f"auth:{scope}:{client_ip}",
                                        AUTH_WINDOW, limit)
    if not allowed:
        from app.logger import log
        log("auth_rate_limited", scope=scope, ip=client_ip)
        raise HTTPException(
            status_code=429,
            detail=(f"Too many attempts: max {limit} per {AUTH_WINDOW}s. "
                    "Try again later."))


def check_setup_rate_limit(client_ip: str) -> None:
    """Bound attempts against the first-owner claim endpoint. Always on.

    SCOPE THIS HONESTLY - it is not a fix for the claim race. /api/auth/setup is
    open until an owner exists, so on a fresh deployment reachable before its
    operator finishes setup, ONE request wins it and no throttle can help.
    Closing that needs a claim secret the deployer holds, which is a deploy-UX
    decision rather than a patch.

    What it does buy, all of it real: password-policy probing and username
    enumeration are bounded, a bulk-reachable endpoint has a bounded blast
    radius, and the "has this instance been claimed yet" oracle costs something
    to ask. The throttle runs BEFORE the owner_exists() check so the closed path
    is bounded too - otherwise the 403 answers that question for free.
    """
    _, allowed = state_store.bump_window(f"setup:{client_ip}",
                                        SETUP_WINDOW, SETUP_MAX_ATTEMPTS)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(f"Too many setup attempts: max {SETUP_MAX_ATTEMPTS} "
                    f"per {SETUP_WINDOW}s"),
        )


# ── First-owner CLAIM CODE (2026-08-27) ───────────────────────────────────────
# The throttle above bounds ATTEMPTS. It cannot close the RACE, and its docstring
# says so: /api/auth/setup is open until an owner exists, so a deployment that is
# publicly reachable before its operator finishes setup goes to whoever asks
# first. One request is all it takes, and one request is under every limit.
#
# THIS IS THE TEMPLATE, which is what makes the race matter here more than
# anywhere. "A fresh deployment, publicly reachable, not yet claimed" is not an
# edge case for this repo - it is the normal first ten minutes of every single
# deployment anyone ever makes from it.
#
# The shape is Jupyter's: a code minted at boot and printed to the container
# logs, which only someone who can already read those logs has seen. No env var
# to forget, no shared default to leak, and it self-disables the moment the
# deployment is claimed.
#
# SETUP_CLAIM_CODE overrides the generated value for operators who provision
# secrets rather than read logs, and it is REQUIRED under multiple workers or
# replicas (see below). There is deliberately no way to turn the requirement
# off: a control whose default state is off reads as guarded in the source and
# is absent in every real deployment.
#
# IN-PROCESS by choice - the one piece of state in this module that stayed so
# when the throttles and the MFA challenge store moved to the database on
# 2026-09-11, because a code minted per process and printed to that process's
# logs IS the design (the caveats below are its terms, not a debt). The
# shipped container runs a single uvicorn process. Under `--workers N` or several
# replicas each would mint a different code and only one would match, so those
# deployments MUST set SETUP_CLAIM_CODE. A restart before the claim mints a fresh
# code and prints it again - correct, because the old one stops working at the
# same moment.
SETUP_CLAIM_CODE_ENV = os.getenv("SETUP_CLAIM_CODE", "").strip()

_claim_code: str | None = None
_claim_code_burned = False


def setup_claim_code() -> str:
    """This deployment's claim code, minted once per process on first read."""
    global _claim_code
    if _claim_code is None:
        _claim_code = SETUP_CLAIM_CODE_ENV or secrets.token_urlsafe(18)
    return _claim_code


def claim_code_source() -> str:
    """Where the live code came from - for the boot line, never the value."""
    return "env" if SETUP_CLAIM_CODE_ENV else "generated"


def verify_setup_claim_code(supplied: str) -> None:
    """Raise 401 unless `supplied` is this deployment's live claim code.

    Compared on BYTES rather than str: secrets.compare_digest raises TypeError
    on a non-ASCII str, and `supplied` is unauthenticated caller input, so a
    single multi-byte character would turn a failed claim into a 500.

    Checked AFTER the owner_exists() 403 in the endpoint, deliberately. Putting
    it first would answer 401 on both a claimed and an unclaimed deployment and
    weaken the claimed-ness oracle further - a real improvement, but a change to
    a control that has its own tests, so it belongs in its own decision.
    """
    if _claim_code_burned:
        raise HTTPException(status_code=401, detail=_CLAIM_CODE_DETAIL)
    expected = setup_claim_code()
    if not secrets.compare_digest((supplied or "").encode("utf-8"),
                                  expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail=_CLAIM_CODE_DETAIL)


_CLAIM_CODE_DETAIL = (
    "Invalid or missing claim code. This deployment prints its code to the "
    "server logs at startup while it is unclaimed.")


def burn_setup_claim_code() -> None:
    """Retire the code once it has bought an owner.

    Belt and braces beside the owner_exists() check: that check reads the
    database, this one is process-local and immediate, and the printed code
    stops working the instant the claim lands rather than the instant the next
    request re-reads the users table.
    """
    global _claim_code_burned
    _claim_code_burned = True


# -- MFA challenge guard (2026-08-27) -----------------------------------------
# /api/auth/mfa/complete had NO attempt counter, NO lockout, and NO invalidation
# of the challenge token after use - while the password path right next to it has
# all three. A valid mfa_token was therefore an unbounded guessing permit for its
# full 5-minute life against a 6-digit code, and valid_window=1 means three codes
# are acceptable at any instant. Re-login minted a fresh window, and the token
# stayed usable after a successful login.
#
# Two bounds, deliberately different in kind:
#   PER CHALLENGE (here) - one sign-in attempt gets a small number of tries, then
#   that challenge is dead. Burned outright on success so it cannot be replayed.
#   PER ACCOUNT (routers/auth.py, reusing the EXISTING lockout) - failures also count
#   toward the same failed_attempts/locked_until the password path uses, so
#   grinding fresh challenges walks into the account lock instead of resetting a
#   counter every time.
#
# In the DATABASE since 2026-09-11 (app/state_store.py), entries living no
# longer than the token they track. Until then this was a dict in the one
# uvicorn process, and a restart forgot burned jtis and attempt counts - a
# completed or exhausted challenge token was honoured fresh for what remained
# of its 5-minute life after a redeploy (the 2026-09-04 audit's "per-process
# auth stores" finding). The store survives restarts and is shared by every
# worker on this node; a second NODE still needs Redis, and the per-ACCOUNT
# lock (also the DB) holds either way.
MFA_MAX_ATTEMPTS  = int(os.getenv("MFA_MAX_ATTEMPTS",  "5"))
MFA_CHALLENGE_TTL = int(os.getenv("MFA_CHALLENGE_TTL", "300"))  # matches the token's exp

def _mfa_key(jti: str) -> str:
    return f"mfa:{jti}"


def _fresh_challenge() -> dict:
    return {"attempts": 0, "used": False, "ts": time.time()}


def check_mfa_challenge(jti: str) -> None:
    """Refuse a burned or exhausted MFA challenge. Raises 401/429, else returns."""
    ch = state_store.get(_mfa_key(jti))
    if ch is None:
        return                                   # first use of this challenge
    if ch["used"]:
        raise HTTPException(
            status_code=401,
            detail="This sign-in has already been completed. Sign in again.")
    if ch["attempts"] >= MFA_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many codes tried for this sign-in. Sign in again.")


def record_mfa_failure(jti: str) -> int:
    """Count a wrong code against this challenge. Returns the new attempt count.
    The row expires when the TOKEN does - MFA_CHALLENGE_TTL from the first
    record, never extended by a later attempt."""
    ch = state_store.update(_mfa_key(jti),
                            lambda c: c.__setitem__("attempts", c["attempts"] + 1),
                            MFA_CHALLENGE_TTL, default=_fresh_challenge())
    return ch["attempts"]


def burn_mfa_challenge(jti: str) -> None:
    """Mark a challenge spent so it can never be replayed - across a restart too."""
    state_store.update(_mfa_key(jti), lambda c: c.__setitem__("used", True),
                       MFA_CHALLENGE_TTL, default=_fresh_challenge())



# ── Daily global guest budget (public-demo wallet backstop) ───────────────────
# Per-IP rate limits don't stop distributed traffic / a busy day; this caps total guest
# requests per UTC day across ALL callers. Redis-backed when available, the
# database otherwise (app/state_store.py, since 2026-09-11 - the in-memory
# counter it replaced forgot the day's count on every restart).
# Tune the limit high enough that real visitors never reach it - it's a backstop, not a gate.


def check_daily_guest_budget(limit: int) -> None:
    if limit <= 0:
        return
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    from app.redis_client import get_redis
    r = get_redis()
    count = None
    if r:
        try:
            key = f"az:guestbudget:{day}"
            count = r.incr(key)
            if count == 1:
                r.expire(key, 90000)  # ~25h, so the daily key self-cleans
        except Exception as e:
            # get_redis() latches its client on first use, so a Redis that dies
            # after a successful ping keeps handing back a live-looking handle
            # and every guest request raises out of this guard. Degrade to the
            # database counter: during an outage the cap loosens from global
            # to per-node, which beats 500ing the lane the control exists to
            # protect. Logged, never silent - a security control that quietly
            # changes scope is the worse failure.
            from app.logger import log_error
            log_error("guest_budget_redis_degraded", error=str(e))
            count = None
    if count is None:
        # Database fallback: one counter per UTC day, expiring ~25h after its
        # first hit so the key self-cleans like the Redis one.
        count = state_store.bump_counter(f"guest_budget:{day}", 90000)
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail="The demo is seeing high demand right now - please try again a little later.",
        )


# ── Prompt injection detection ────────────────────────────────────────────────
ENABLE_INJECTION_PROTECTION = os.getenv("ENABLE_INJECTION_PROTECTION", "true").lower() == "true"

_INJECTION_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
        r"forget\s+(everything|all\s+previous)",
        r"disregard\s+your\s+(instructions|training|guidelines|system\s+prompt)",
        r"you\s+are\s+now\s+(?:a\s+)?(?:different|new|unrestricted|evil|an?\s+AI\s+without)",
        r"\bdo\s+anything\s+now\b",
        r"\bDAN\b",
        r"act\s+as\s+(if\s+you\s+are\s+)?(?:an?\s+)?(?:unrestricted|unfiltered|evil|jailbroken)",
        r"pretend\s+you\s+have\s+no\s+restrictions",
        r"override\s+(your\s+)?(system\s+prompt|instructions|safety\s+guidelines)",
        r"(system|assistant)\s*:\s*you\s+are",  # role-injection via colon syntax
    ]
]


def check_injection(prompt: str) -> None:
    if not ENABLE_INJECTION_PROTECTION:
        return
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(prompt):
            raise HTTPException(
                status_code=400,
                detail="Request blocked: potential prompt injection detected",
            )


def get_security_config() -> dict:
    return {
        "rate_limit_enabled": ENABLE_RATE_LIMIT,
        "rate_limit_requests": RATE_LIMIT_REQUESTS,
        "rate_limit_window": RATE_LIMIT_WINDOW,
        "injection_protection": ENABLE_INJECTION_PROTECTION,
    }
