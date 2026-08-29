#!/usr/bin/env python3
"""Scheduled backup, run INSIDE the backend container.

    docker compose exec -T backend python /app/scripts/backup_cron.py

WHY NOT CURL THE ENDPOINT. POST /api/admin/backup is Owner-gated, and the only
credential this platform issues is a user access token that expires in
JWT_ACCESS_EXPIRE_MINUTES (default 30). A nightly job cannot hold one: it would
work once and 401 every night after. That is the same wall the Prometheus
scrape hit, and /metrics solved it with a long-lived METRICS_TOKEN - but a
backup is a WRITE that snapshots the whole data directory, so minting a
never-expiring credential for it would be a much larger grant than the job
needs. Running in the container skips the question: there is no network hop to
authenticate, and the process already has the filesystem access the work needs.

WHAT IT WRITES, and why the second half matters as much as the first. Besides
the archive, this writes backup-status.json into the data dir. GET
/api/backup-status reads that file and answers 503 when it is missing, stale, or
records a failure - it is deliberately unauthenticated so an uptime prober can
watch it. The endpoint does NOT write the file itself, so a backup job that
only creates archives leaves the health probe alarming forever, and an operator
who has read the admin card blames the probe rather than believing their
backups. A failed run is recorded too, with ok=false, because a job that stops
running silently is the failure this file exists to make loud.

Exit code is 0 on success and 1 on failure, so cron's own mail-on-failure and
any wrapper both see the truth.
"""
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, "/app")

STATUS_DIR = os.getenv("BACKUP_STATUS_DIR", "/app/data")
STATUS_FILE = os.path.join(STATUS_DIR, "backup-status.json")
# The exact format _backup_job_state parses (system.py). Not ISO-8601: it uses
# %Y-%m-%dT%H%M%SZ, and a mismatch here reads as "unparseable last_success",
# which alarms with a message that sounds like corruption rather than a format
# disagreement.
_TS = "%Y-%m-%dT%H%M%SZ"


def _write_status(ok: bool, detail: str) -> None:
    payload = {"ok": ok, "detail": detail}
    if ok:
        payload["last_success"] = _dt.datetime.now(_dt.timezone.utc).strftime(_TS)
    else:
        # Preserve the previous last_success so the probe can tell "failed once,
        # still has a fresh backup" from "has never worked" - the first is a
        # warning, the second is an emergency, and collapsing them loses the
        # distinction exactly when it matters.
        try:
            with open(STATUS_FILE) as f:
                prev = json.load(f).get("last_success")
            if prev:
                payload["last_success"] = prev
        except Exception:
            pass
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, STATUS_FILE)   # atomic: a probe never reads a half-written file


def main() -> int:
    try:
        from app.routers.admin import run_backup
        # run_backup takes the caller only to name it in the audit line; there
        # is no request here, so it is named honestly rather than impersonating
        # an operator who did not press anything.
        result = run_backup({"id": None, "username": "backup-cron"})
    except Exception as e:
        _write_status(False, f"{type(e).__name__}: {e}")
        print(f"backup FAILED: {e}", file=sys.stderr)
        return 1
    _write_status(True, result.get("file", ""))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
