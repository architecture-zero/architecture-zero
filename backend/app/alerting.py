import os
import time
import threading
import smtplib
from email.message import EmailMessage

import requests as _req
from app.logger import log, log_error

_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
_EMAIL_TO    = os.getenv("ALERT_EMAIL", "")
_SMTP_HOST   = os.getenv("ALERT_SMTP_HOST", "")
_SMTP_PORT   = int(os.getenv("ALERT_SMTP_PORT", "587"))
_SMTP_USER   = os.getenv("ALERT_SMTP_USER", "")
_SMTP_PASS   = os.getenv("ALERT_SMTP_PASS", "")
_EMAIL_FROM  = os.getenv("ALERT_FROM_EMAIL", "") or _SMTP_USER

DISK_ALERT_THRESHOLD_PCT = int(os.getenv("DISK_ALERT_THRESHOLD_PCT", "85"))
_COOLDOWN                = int(os.getenv("ALERT_COOLDOWN_SECONDS", "3600"))

_last: dict[str, float] = {}
_lock = threading.Lock()


def _cooldown_ok(key: str) -> bool:
    with _lock:
        if time.time() - _last.get(key, 0) < _COOLDOWN:
            return False
        _last[key] = time.time()
        return True


def _webhook(title: str, body: str) -> None:
    if not _WEBHOOK_URL:
        return
    try:
        _req.post(_WEBHOOK_URL, json={"text": f"*{title}*\n{body}"}, timeout=5)
        log("alert_webhook_sent", title=title)
    except Exception as e:
        log_error("alert_webhook_failed", error=str(e))


def _email(subject: str, body: str) -> None:
    if not (_EMAIL_TO and _SMTP_HOST and _SMTP_USER and _SMTP_PASS):
        return
    try:
        msg = EmailMessage()
        msg["From"] = _EMAIL_FROM
        msg["To"]   = _EMAIL_TO
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=10) as s:
            s.starttls()
            s.login(_SMTP_USER, _SMTP_PASS)
            s.send_message(msg)
        log("alert_email_sent", to=_EMAIL_TO)
    except Exception as e:
        log_error("alert_email_failed", error=str(e))


_POOL = None
_POOL_GUARD = threading.Lock()


def _pool():
    """Lazily built so importing this module starts no threads - alerting is
    dormant on an instance with no webhook or SMTP configured."""
    global _POOL
    with _POOL_GUARD:
        if _POOL is None:
            from concurrent.futures import ThreadPoolExecutor
            _POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alert")
        return _POOL


def _send_all(title: str, body: str) -> None:
    _webhook(title, body)
    _email(title, body)


def fire(key: str, title: str, body: str) -> None:
    """Send a deduped alert (per cooldown window) via all configured channels.

    daemon=False on purpose: daemon threads die with the process, so an exit
    right after a disk-threshold alert drops it silently - the alert path is
    least reliable exactly when the box is in trouble. Non-daemon delivery
    makes interpreter shutdown wait for the send, and both channels are
    timeout-bounded (5s webhook + 10s SMTP) so the wait is too. Still off the
    caller's thread - a health-check request never blocks on SMTP.

    BOUNDED: spawning a thread per alert traded one silent failure for an
    unbounded one - nothing capped how many delivery threads could exist at
    once. The ThreadPoolExecutor caps the workers and keeps the property
    daemon=False was chosen for: pool workers are non-daemon and
    concurrent.futures registers an atexit join, so interpreter shutdown
    still waits for an in-flight send. Two workers is enough for two
    channels; the per-key cooldown is what keeps the submit queue short."""
    if not _cooldown_ok(key):
        return
    _pool().submit(_send_all, title, body)


def get_config() -> dict:
    return {
        "webhook_configured": bool(_WEBHOOK_URL),
        "email_configured":   bool(_EMAIL_TO and _SMTP_HOST),
        "disk_threshold_pct": DISK_ALERT_THRESHOLD_PCT,
        "cooldown_seconds":   _COOLDOWN,
    }
