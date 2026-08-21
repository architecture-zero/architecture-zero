import re
import time
import os
import ipaddress
from collections import defaultdict
from fastapi import HTTPException, Request

# ── Rate limiting ─────────────────────────────────────────────────────────────
ENABLE_RATE_LIMIT    = os.getenv("ENABLE_RATE_LIMIT",    "false").lower() == "true"
RATE_LIMIT_REQUESTS  = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW    = int(os.getenv("RATE_LIMIT_WINDOW",   "60"))   # seconds

_rate_store: dict[str, list[float]] = defaultdict(list)
# Keys are never removed by the per-IP prune below - that only trims one IP's
# timestamps, and only for an IP currently making a request. Every address that
# ever hit this process keeps its entry forever, so on a public endpoint the
# dict grows with the count of distinct source IPs seen since boot: scanners,
# crawlers, one-shot probes. Small per key, unbounded in total.
#
# The Redis path never had this - its keys carry an EXPIRE. This is the
# memory-store fallback catching up, which matters because that fallback is
# exactly what runs when Redis is down.
_RATE_SWEEP_EVERY = 1000
_rate_calls_since_sweep = 0


def _sweep_rate_store(now: float) -> int:
    """Drop IPs with nothing left inside the window. Returns how many went."""
    cutoff = now - RATE_LIMIT_WINDOW
    dead = [ip for ip, ts in _rate_store.items() if not ts or max(ts) <= cutoff]
    for ip in dead:
        _rate_store.pop(ip, None)
    return len(dead)


def _check_rate_limit_memory(client_ip: str) -> None:
    global _rate_calls_since_sweep
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    # Amortized sweep rather than a background timer: no extra thread, and the
    # cost lands on the traffic that caused the growth.
    _rate_calls_since_sweep += 1
    if _rate_calls_since_sweep >= _RATE_SWEEP_EVERY:
        _rate_calls_since_sweep = 0
        _sweep_rate_store(now)
    timestamps = [t for t in _rate_store[client_ip] if t > cutoff]
    if len(timestamps) >= RATE_LIMIT_REQUESTS:
        # Refused requests still count - do not let the store grow a key for an
        # IP being actively rejected without it also being sweepable.
        _rate_store[client_ip] = timestamps
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW}s",
        )
    timestamps.append(now)
    _rate_store[client_ip] = timestamps


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
        _check_rate_limit_memory(client_ip)


# ── Daily global guest budget (public-demo wallet backstop) ───────────────────
# Per-IP rate limits don't stop distributed traffic / a busy day; this caps total guest
# requests per UTC day across ALL callers. Redis-backed when available, in-memory otherwise.
# Tune the limit high enough that real visitors never reach it - it's a backstop, not a gate.
_daily_guest_store: dict[str, int] = {}


def check_daily_guest_budget(limit: int) -> None:
    if limit <= 0:
        return
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    from app.redis_client import get_redis
    r = get_redis()
    if r:
        key = f"az:guestbudget:{day}"
        count = r.incr(key)
        if count == 1:
            r.expire(key, 90000)  # ~25h, so the daily key self-cleans
    else:
        # In-memory fallback: keep only today's counter.
        for k in [k for k in _daily_guest_store if k != day]:
            _daily_guest_store.pop(k, None)
        _daily_guest_store[day] = _daily_guest_store.get(day, 0) + 1
        count = _daily_guest_store[day]
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
