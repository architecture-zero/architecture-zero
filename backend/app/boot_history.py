"""Boot history - the crash-loop signal behind /api/health/ready (2026-08-22).

Why this exists, precisely:

On 2026-08-20 uvicorn segfaulted inside hnswlib every ~5.5 minutes for 10.5
hours - RestartCount 97, chat 500ing the whole window - and **not one alert
fired**. The uptime checks probe liveness, and a process that crash-loops
still serves 200s between deaths, so the monitor stayed green while the product
was dead. That was upstream; this module was ported here 2026-10-08.

That incident also settled what this module must NOT be. The write-side
corruption cannot be probed from inside the process, because "the probe would BE
the segfault" - which is why the fix there was an operator lever
(force-rebuild.json), not a detector. So this does not touch Chroma, does not
query an index, and cannot itself crash anything. It counts boots.

The trick is telling a crash loop from a busy deploy day, since both restart the
container repeatedly. GIT_SHA separates them: every deploy carries a NEW sha, so
N deploys leave N distinct shas, while a crash loop hammers the SAME sha over
and over. On 2026-08-22 alone there were four deploys in about ninety minutes
and that is normal; the incident was roughly eleven boots an hour on ONE sha.
Counting same-sha boots means the threshold separates the two cases by kind
rather than by a number tuned to sit between them.

GIT_SHA is a BUILD ARG here (backend/Dockerfile bakes it into the image; the
shipped compose file forwards `${GIT_SHA:-unknown}` from the shell that runs
the build). An image built without it stamps every boot "unknown", so rebuilds
stop counting as new builds and a busy hour of them reads as a loop: build
with the commit wherever readiness is watched (docs/runbook.md, Monitoring).

FAIL-OPEN, deliberately: a missing, unreadable or malformed history file reports
zero, never "unhealthy". A monitoring input that invents outages gets muted by
its owner, and a muted monitor is worth less than no monitor. The failure this
guards against is silence during a real loop, not a missing file.
"""
import json
import os
import time

DATA_DIR = os.getenv("BOOT_HISTORY_DIR", os.getenv("BACKUP_STATUS_DIR", "/app/data"))
HISTORY_FILE = "boot-history.json"
KEEP = 50
# ~11 same-sha boots/hour was the incident; 4 deploys in 90 minutes was a normal
# busy day but they carried four DIFFERENT shas, so they score 1 each here.
LOOP_THRESHOLD = int(os.getenv("BOOT_LOOP_THRESHOLD", "4"))
WINDOW_SECONDS = int(os.getenv("BOOT_LOOP_WINDOW_SECONDS", "3600"))


def _path() -> str:
    return os.path.join(DATA_DIR, HISTORY_FILE)


def _read() -> list[dict]:
    try:
        with open(_path()) as f:
            data = json.load(f)
        return [b for b in data if isinstance(b, dict) and "ts" in b] if isinstance(data, list) else []
    except Exception:
        return []


def record_boot(sha: str | None = None, now: float | None = None) -> None:
    """Append this boot. Never raises - a failure here must not stop startup."""
    try:
        sha = sha if sha is not None else os.getenv("GIT_SHA", "unknown")
        entry = {"ts": now if now is not None else time.time(), "sha": sha}
        history = (_read() + [entry])[-KEEP:]
        tmp = _path() + ".tmp"
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(history, f)
        os.replace(tmp, _path())
    except Exception:
        pass


def crash_loop_state(now: float | None = None) -> dict:
    """{'looping': bool, 'boots': int, 'sha': str, 'window_s': int}.

    'boots' counts boots of the CURRENT sha inside the window - see the module
    docstring for why same-sha is the discriminator. Reports, never raises.
    """
    now = now if now is not None else time.time()
    sha = os.getenv("GIT_SHA", "unknown")
    try:
        recent = [b for b in _read()
                  if b.get("sha") == sha and (now - float(b.get("ts", 0))) <= WINDOW_SECONDS]
        n = len(recent)
    except Exception:
        n = 0
    return {"looping": n > LOOP_THRESHOLD, "boots": n, "sha": sha, "window_s": WINDOW_SECONDS}
