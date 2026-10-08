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
the build). An image built without it stamps every boot "unknown", which
cannot tell one build from the next, so then the check does not watch at all
- readiness shows `crash_loop: unwatched` and passes - rather than read a
setup hour of rebuilds as a loop: build with the commit wherever readiness is
watched (docs/runbook.md, Monitoring).

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


def current_sha() -> str:
    """This build's GIT_SHA, as both the stamp and the check read it: stripped,
    "unknown" when absent or blank (a padded value was stamped raw and compared
    stripped, so no boot ever matched - the 2026-10-08 read)."""
    return os.getenv("GIT_SHA", "unknown").strip() or "unknown"


def _boot_time(entry: dict) -> float | None:
    """One entry's timestamp, or None for one this module never wrote."""
    try:
        return float(entry.get("ts", 0))
    except (TypeError, ValueError):
        return None


def record_boot(sha: str | None = None, now: float | None = None) -> None:
    """Append this boot. Never raises - a failure here must not stop startup."""
    try:
        sha = sha if sha is not None else current_sha()
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
    """{'looping': bool, 'watched': bool, 'boots': int, 'sha': str, 'window_s': int}.

    'boots' counts boots of the CURRENT sha inside the window - see the module
    docstring for why same-sha is the discriminator. Reports, never raises.
    """
    now = now if now is not None else time.time()
    sha = current_sha()
    if sha == "unknown":
        # Built without its commit, every build stamps the same "unknown", so a
        # setup hour of rebuilds would read as one build crash-looping and fail
        # readiness for an hour. With no build to tell apart the check does not
        # watch ('watched' False; readiness shows "unwatched") - fail open,
        # never an invented outage. Building with GIT_SHA turns it on.
        return {"looping": False, "watched": False, "boots": 0, "sha": sha,
                "window_s": WINDOW_SECONDS}
    try:
        # Per entry: one this module never wrote (a non-numeric ts) is skipped,
        # rather than zeroing the whole count (the 2026-10-08 read).
        stamps = [_boot_time(b) for b in _read() if b.get("sha") == sha]
        n = sum(1 for t in stamps if t is not None and now - t <= WINDOW_SECONDS)
    except Exception:
        n = 0
    return {"looping": n > LOOP_THRESHOLD, "watched": True, "boots": n, "sha": sha,
            "window_s": WINDOW_SECONDS}
