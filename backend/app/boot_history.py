"""Boot history - the crash-loop signal behind /api/health/ready (2026-08-22).

Why this exists, precisely:

On 2026-08-20 a deployment of this stack segfaulted inside hnswlib every ~5.5
minutes for 10.5 hours - RestartCount 97, chat 500ing the whole window - and
**not one alert fired**. The uptime checks probe liveness, and a process that
crash-loops still serves 200s between deaths, so the monitor stayed green while
the product was dead.

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

GIT_SHA comes from the build: backend/Dockerfile bakes its GIT_SHA build arg
into the image, and the compose file forwards `${GIT_SHA:-unknown}` from the
shell that runs the build. An image built without it stamps every boot
"unknown", which cannot tell one build from the next, so then the check does
not watch at all - readiness shows `crash_loop: unwatched` and passes - rather
than read a setup hour of rebuilds as a loop. Build with the commit wherever
readiness is watched.

FAIL-OPEN, deliberately: a missing, unreadable or malformed history file reports
zero, never "unhealthy". A monitoring input that invents outages gets muted by
its owner, and a muted monitor is worth less than no monitor. The failure this
guards against is silence during a real loop, not a missing file.

BUT NEVER SILENTLY BLIND (2026-10-08). A process whose own boot stamp could not
be written - an unwritable BOOT_HISTORY_DIR - cannot count its own restarts,
and it read "ok" forever. It reads "unrecorded" now: still passing, but said -
in readiness, in the boot's log line and in the Owner's detailed view. The
readiness word is logged when it changes, never per request (readiness is
polled, and the access log already has every hit).

The same text on every surface of this stack: a surface's first startup hook
calls record_boot() and then log_boot(), its readiness route asks
crash_loop_readiness() and its detailed route crash_loop_status().
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

# Whether THIS process's boot stamp was written: None until record_boot runs.
_stamped: bool | None = None
# The crash_loop word readiness last logged, so it logs on change.
_ready_logged: str | None = None


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


def record_boot(sha: str | None = None, now: float | None = None) -> bool:
    """Append this boot, and remember and return whether it was written. Never
    raises - a failure here must not stop startup. A stamp that could not be
    written leaves this process "unrecorded" (the module docstring)."""
    global _stamped
    try:
        sha = sha if sha is not None else current_sha()
        entry = {"ts": now if now is not None else time.time(), "sha": sha}
        history = (_read() + [entry])[-KEEP:]
        tmp = _path() + ".tmp"
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(history, f)
        os.replace(tmp, _path())
        _stamped = True
    except Exception:
        _stamped = False
    return _stamped


def crash_loop_state(now: float | None = None) -> dict:
    """{'looping': bool, 'watched': bool, 'recorded': bool, 'boots': int,
    'sha': str, 'window_s': int}.

    'boots' counts boots of the CURRENT sha inside the window - see the module
    docstring for why same-sha is the discriminator. 'recorded' is False only
    when this process's own stamp failed (record_boot). Reports, never raises.
    """
    now = now if now is not None else time.time()
    sha = current_sha()
    recorded = _stamped is not False
    if sha == "unknown":
        # Built without its commit, every build stamps the same "unknown", so a
        # setup hour of rebuilds would read as one build crash-looping and fail
        # readiness for an hour. With no build to tell apart the check does not
        # watch ('watched' False; readiness shows "unwatched") - fail open,
        # never an invented outage. Building with GIT_SHA turns it on.
        return {"looping": False, "watched": False, "recorded": recorded, "boots": 0,
                "sha": sha, "window_s": WINDOW_SECONDS}
    try:
        # Per entry: one this module never wrote (a non-numeric ts) is skipped,
        # rather than zeroing the whole count (the 2026-10-08 read).
        stamps = [_boot_time(b) for b in _read() if b.get("sha") == sha]
        n = sum(1 for t in stamps if t is not None and now - t <= WINDOW_SECONDS)
    except Exception:
        n = 0
    return {"looping": n > LOOP_THRESHOLD, "watched": True, "recorded": recorded,
            "boots": n, "sha": sha, "window_s": WINDOW_SECONDS}


def _word(state: dict) -> tuple[str, bool]:
    """One state's crash_loop word, and whether it fails readiness. Only a loop
    fails it; "unwatched" (no commit to tell builds apart) and "unrecorded" (this
    boot's stamp was not written) pass and say so - fail open, never silent."""
    if state["looping"]:
        return f"looping ({state['boots']} boots/{state['window_s']}s)", True
    if not state["watched"]:
        return "unwatched", False
    if not state["recorded"]:
        return "unrecorded", False
    return "ok", False


def crash_loop_readiness(now: float | None = None) -> tuple[str, bool]:
    """The word /api/health/ready shows, and whether it fails readiness:
    "looping (N boots/Ws)" (fails), "ok", "unwatched", "unrecorded", or
    "unavailable" when the check itself could not run (passes - fail open,
    never an invented outage). Logged when the word changes, never per
    request. Never raises."""
    global _ready_logged
    try:
        state = crash_loop_state(now)
        word, failing = _word(state)
    except Exception as e:
        state, word, failing = {"error": type(e).__name__}, "unavailable", False
    if word != _ready_logged:
        _ready_logged = word
        try:
            from app.logger import log, log_error
            if failing:
                log_error("readiness_crash_loop_failing", word=word, sha=state["sha"],
                          boots=state["boots"], window_s=state["window_s"])
            elif word == "unavailable":
                log_error("readiness_crash_loop_unavailable", error=state["error"])
            else:
                log("readiness_crash_loop", word=word)
        except Exception:
            pass
    return word, failing


def crash_loop_status(now: float | None = None) -> dict:
    """The Owner's view: the readiness word with what it was read from - one
    read of the history, so the word and the count cannot come from two reads.
    For the authenticated detailed route only (it names the build). Never
    raises."""
    try:
        state = crash_loop_state(now)
        word, failing = _word(state)
    except Exception:
        return {"state": "unavailable", "failing": False}
    return {"state": word, "failing": failing, "watched": state["watched"],
            "recorded": state["recorded"], "boots": state["boots"],
            "threshold": LOOP_THRESHOLD, "window_s": state["window_s"],
            "sha": state["sha"]}


def log_boot() -> None:
    """The boot's one log line, after record_boot: whether the crash-loop check
    watches this process, so an operator who expects it can see it is off -
    "unwatched" and "unrecorded" otherwise show only as a word in an anonymous
    readiness body (the 2026-10-08 read). Never raises."""
    try:
        from app.logger import log, log_error
        state = crash_loop_state()
        check = "watched" if state["watched"] else "unwatched - built without GIT_SHA"
        if state["recorded"]:
            log("boot_recorded", sha=state["sha"], crash_loop_check=check)
        else:
            # This process cannot see its own restarts; readiness says "unrecorded".
            log_error("boot_unrecorded", sha=state["sha"], crash_loop_check=check,
                      reason=f"the boot stamp could not be written in {DATA_DIR}")
    except Exception:
        pass
