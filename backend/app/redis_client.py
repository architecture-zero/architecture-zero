"""
Optional Redis client - lazy singleton.

Returns None when REDIS_URL is not set, allowing every caller to fall back to
the in-process / SQLite alternative without changing control flow.

Usage:
    from app.redis_client import get_redis

    r = get_redis()
    if r:
        r.set("key", "value")
    else:
        # in-memory / DB fallback
"""

import logging
import os

log = logging.getLogger(__name__)

_client = None
_initialized = False


def get_redis():
    """Return a connected Redis client, or None if Redis is not configured / reachable."""
    global _client, _initialized
    if _initialized:
        return _client

    _initialized = True
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None

    try:
        import redis as _redis  # optional dependency
        client = _redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        client.ping()
        log.info("Redis connected: %s", url)
        _client = client
    except Exception as exc:
        log.warning("Redis unavailable (%s) - falling back to in-memory/DB", exc)
        _client = None

    return _client


def redis_status() -> str:
    """Return 'connected', 'disabled', or 'unreachable'."""
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return "disabled"
    r = get_redis()
    if r:
        try:
            r.ping()
            return "connected"
        except Exception:
            return "unreachable"
    return "unreachable"
