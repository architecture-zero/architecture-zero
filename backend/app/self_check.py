"""The instance checks itself (2026-10-02; the upgrade rehearsal's leg 5).

Until this module the disk and Ollama alerts were raised inside
GET /api/health/detailed and nowhere else, so they ran on the Monitoring tab's
30-second timer while an Owner had it open, and not at all otherwise. A box
nobody was watching sent nothing: the 2026-09-29 rehearsal stopped Ollama and
waited five minutes with nobody signed in - no alert - then made one Owner
read of the route, and the alert arrived within the second.

Now the same probes run on a timer from boot, through the same alert channels
and the same per-key cooldown (app.alerting). The detailed route calls the
same probes, so what an Owner sees on the Monitoring tab is what the timer
checks.

What it watches is decided once, by the deployment's startup hook, which
passes it in: the data volume's disk use always; Ollama through the given
reader when Ollama is enabled (None: not watched here); and the backup job's
and the restore drill's heartbeats through the given status reader - the same
function a public /api/backup-status probe serves, so the alarm and the probe
cannot disagree (None: this deployment's backups are watched elsewhere).
Everything else an operator wants watched from outside is in the runbook's
Monitoring section. This file is the same on every surface that carries it.

The retrieval lane (AZ-02, outside review 2026-10-06; fixed 2026-10-07):
readiness proved the database and nothing else, so an instance whose embed
service was down or misconfigured read ready while every governed query
failed. The startup hook passes the deployment's lane probe
(database.probe_retrieval_lane); each pass records what it found, an error
alerts like the others, and /api/health/ready reads the last pass through
rag_readiness() - never probing inline, because the probe reaches the embed
service and the vector store and an unauthenticated route must stay cheap and
inert. A last pass older than two intervals plus a minute reads "stale" and
fails readiness the same as an error: a lane nobody has proven lately is not
proven. Before the first pass it reads "pending"; with the timer off, "off".

SELF_CHECK_INTERVAL_SECONDS (default 300; 0 turns the timer off). The first
run comes one interval after boot, so a restart is not itself an alert.
SELF_CHECK_BACKUP (default true) - false on a deployment that backs up some
other way and writes no backup-status.json. Alerts leave the box only when a
channel is configured (ALERT_WEBHOOK_URL, or the SMTP set): an instance with
none runs the probes and logs what changed, nothing else.
"""
import asyncio
import os
import shutil
import time

from app.alerting import DISK_ALERT_THRESHOLD_PCT, fire as fire_alert
from app.logger import log, log_error

SELF_CHECK_INTERVAL_SECONDS = int(os.getenv("SELF_CHECK_INTERVAL_SECONDS", "300"))
SELF_CHECK_BACKUP = os.getenv("SELF_CHECK_BACKUP", "true").lower() == "true"
_INSTANCE_NAME = os.getenv("VITE_INSTANCE_NAME", "Architecture Zero")


def ollama_enabled() -> bool:
    return os.getenv("ENABLE_OLLAMA", "true").lower() == "true"


def probe_disk(data_dir: str) -> dict:
    """Disk use of the data volume, and the alert over the threshold."""
    try:
        usage = shutil.disk_usage(data_dir)
    except Exception as e:
        return {"error": str(e)}
    pct = round(usage.used / usage.total * 100, 1)
    result = {"used_gb": round(usage.used / 1e9, 2),
              "total_gb": round(usage.total / 1e9, 2),
              "pct": pct, "ok": pct < DISK_ALERT_THRESHOLD_PCT}
    if not result["ok"]:
        fire_alert("disk_high", f"Disk usage high - {_INSTANCE_NAME}",
                   f"Disk at {pct}% ({result['used_gb']} GB used)")
    return result


def probe_ollama(ollama_get) -> dict:
    """Ollama's model list within 3 s, and the alert when it does not answer."""
    try:
        t0 = time.perf_counter()
        r = ollama_get("/api/tags", timeout=3)
        return {"name": "ollama", "ok": r.status_code == 200,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
    except Exception:
        fire_alert("ollama_down", f"Ollama unreachable - {_INSTANCE_NAME}",
                   "Ollama did not respond within 3s. Chat will fail for Ollama models.")
        return {"name": "ollama", "ok": False, "latency_ms": None}


def probe_backups(backup_state, kinds=("backup", "drill")) -> dict:
    """The heartbeats named in `kinds` (each read from <kind>-status.json:
    by default the backup job's and the restore drill's) through the given
    status reader; an alert for each one that is not ok. A deployment that
    runs no drill passes ("backup",)."""
    states = {kind: backup_state(f"{kind}-status.json") for kind in kinds}
    for kind, st in states.items():
        if not st.get("ok"):
            age = st.get("age_hours")
            fire_alert(f"backup_{kind}", f"Backup alarm: {kind} not ok - {_INSTANCE_NAME}",
                       f"{kind}: {st.get('reason', 'unknown')}"
                       + (f" (age {age}h)" if age is not None else "")
                       + ". The host job writes its status into the data directory;"
                         " the runbook's Backups section.")
    return states


# The retrieval lane's last pass ({"state", "reason", "at"}), whether the
# startup hook wired a probe, and when the timer started - what readiness reads.
# Each is rebound whole, never mutated, so a reader never sees half a pass.
_rag_last: dict | None = None
_rag_wired = False
_started_at: float | None = None


def _rag_stale_after() -> float:
    return 2 * SELF_CHECK_INTERVAL_SECONDS + 60


def probe_rag(rag_probe) -> dict:
    """One pass of the retrieval-lane probe the startup hook wired; recorded
    for readiness, and an alert when it fails."""
    global _rag_last
    try:
        found = rag_probe()
        state, reason = found["state"], found.get("reason")
    except Exception as e:
        log_error("self_check_rag_probe_crashed", error=str(e)[:300])
        state, reason = "error", "probe_crashed"
    if state == "error":
        fire_alert("rag_down", f"Retrieval lane failing - {_INSTANCE_NAME}",
                   f"The retrieval lane failed its check ({reason}). Answers that "
                   "need the knowledge base will fail until it recovers - the "
                   "runbook's Monitoring section.")
    _rag_last = {"state": state, "reason": reason, "at": time.time()}
    return {"state": state, "reason": reason}


def rag_readiness(now: float | None = None) -> tuple[str, bool]:
    """(the word /api/health/ready shows for the lane, whether it fails
    readiness). Reads the last pass; never probes."""
    if not _rag_wired:
        return "skipped", False
    if SELF_CHECK_INTERVAL_SECONDS <= 0:
        return "off", False
    now = time.time() if now is None else now
    last = _rag_last
    if last is None:
        if _started_at is not None and now - _started_at <= _rag_stale_after():
            return "pending", False
        return "stale", True
    if now - last["at"] > _rag_stale_after():
        return "stale", True
    return last["state"], last["state"] == "error"


def rag_status() -> dict:
    """The Owner's view of the lane: the readiness word plus the last pass's
    reason and time. Never on an unauthenticated route."""
    word, failing = rag_readiness()
    last = _rag_last
    return {"state": word, "failing": failing,
            "reason": last.get("reason") if last else None,
            "checked_at": round(last["at"]) if last else None}


# What the last pass found failing, and when it was last written down: the log
# says when a check starts failing and when it recovers, plus a reminder each
# alert cooldown while it stays down - not a line every interval.
_last_failing: tuple = ()
_last_logged: float = 0.0


def _note(failing: list) -> None:
    global _last_failing, _last_logged
    from app.alerting import get_config as _alert_config
    now = time.time()
    if tuple(failing) != _last_failing:
        if failing:
            log("self_check_failing", checks=failing)
        else:
            log("self_check_recovered", checks=list(_last_failing))
        _last_failing, _last_logged = tuple(failing), now
    elif failing and now - _last_logged >= _alert_config()["cooldown_seconds"]:
        log("self_check_failing", checks=failing, reminder=True)
        _last_logged = now


def run_self_check(data_dir: str, ollama_get=None, backup_state=None,
                   backup_kinds=("backup", "drill"), rag_probe=None) -> dict:
    """One pass of every probe this deployment watches; the log says what changed."""
    result = {"disk": probe_disk(data_dir)}
    if ollama_get is not None and ollama_enabled():
        result["ollama"] = probe_ollama(ollama_get)
    if backup_state is not None and SELF_CHECK_BACKUP:
        result["backups"] = probe_backups(backup_state, backup_kinds)
    if rag_probe is not None:
        result["rag"] = probe_rag(rag_probe)
    failing = []
    if not result["disk"].get("ok"):
        failing.append("disk")
    if "ollama" in result and not result["ollama"]["ok"]:
        failing.append("ollama")
    if result.get("rag", {}).get("state") == "error":
        failing.append("rag")
    for kind, st in result.get("backups", {}).items():
        if not st.get("ok"):
            failing.append(kind)
    _note(failing)
    return result


async def self_check_loop(data_dir: str, ollama_get=None, backup_state=None,
                          backup_kinds=("backup", "drill"), rag_probe=None) -> None:
    """The timer. Started from the startup hook; the probes run off the event
    loop (a blocking disk or HTTP read must never stall a request)."""
    global _rag_wired, _started_at
    _rag_wired, _started_at = rag_probe is not None, time.time()
    interval = SELF_CHECK_INTERVAL_SECONDS
    if interval <= 0:
        log("self_check_off")
        return
    log("self_check_started", interval_seconds=interval,
        ollama=ollama_get is not None and ollama_enabled(),
        backups=backup_state is not None and SELF_CHECK_BACKUP,
        rag=rag_probe is not None)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(run_self_check, data_dir, ollama_get, backup_state,
                                    backup_kinds, rag_probe)
        except Exception as e:
            log_error("self_check_crashed", error=str(e)[:300])
