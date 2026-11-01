"""The file reader's locked box: every PDF and Word file is parsed here.

The backend hands a file over a Unix socket and gets its text back. This
container has no network, no secrets, no mounts but the socket folder, a
read-only filesystem, no Linux capabilities and a non-root user
(docker-compose.yml's extractor service). On top of that, each file is parsed
in a fresh child process (worker.py) with a wall clock, a CPU clock and a
memory cap, so a file built to hang or to eat memory loses only its own
process. A file that broke the parser outright would take over an empty box:
the most it could do is answer with false text, which then meets the same
injection checks as any other untrusted text.

The wire format, one request per connection:
  -> one JSON line {"v": 1, "ext": "pdf" | "docx", "size": N}, then N bytes
  <- one JSON line, worker.py's answer, or {"ok": false, "kind": K} where K
     is timeout, memory, too_large, busy or bad_request (see worker.py for
     the kinds the parse itself reports)
"""
import json
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time

SOCKET_PATH = os.environ.get("EXTRACTOR_SOCKET", "/run/extractor/extractor.sock")

# The per-file limits (decided 2026-11-01): a normal 50-page PDF parses in
# well under a second, so these catch only files built to attack.
TIMEOUT_S = 20
MEMORY_BYTES = 512 * 1024 * 1024
# The largest upload any deployment allows; a larger file is refused unread.
MAX_INPUT_BYTES = 64 * 1024 * 1024
# The most a child may write back: worker.MAX_TEXT_CHARS of text, JSON-escaped
# (up to 6 bytes a character), with room to spare.
MAX_ANSWER_BYTES = 40 * 1024 * 1024
# One file at a time; a second waits this long for the slot, then is told busy
# (the caller treats busy as the server's fault and tries again later).
CONCURRENCY = 1
SLOT_WAIT_S = 30
# How long a connection may take to send its request.
READ_TIMEOUT_S = 30

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
WORKER_COMMAND = [sys.executable, "-I", WORKER]
# The child gets an environment of its own, not this process's.
CHILD_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
SUPPORTED = ("pdf", "docx")

_slots = threading.BoundedSemaphore(CONCURRENCY)


def _limit_child() -> None:
    """Runs in the child between fork and exec (POSIX only)."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S + 5, TIMEOUT_S + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))   # writes no file at all
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _classify_exit(rc: int, stderr: bytes) -> dict:
    if b"MemoryError" in stderr[-4096:]:
        return {"ok": False, "kind": "memory", "detail": "MemoryError"}
    if rc == -signal.SIGXCPU:
        return {"ok": False, "kind": "timeout", "detail": "CPU limit"}
    if rc == -signal.SIGKILL:
        return {"ok": False, "kind": "memory", "detail": "killed (memory)"}
    return {"ok": False, "kind": "unreadable", "detail": f"worker exit {rc}"}


def _kill_group(proc: subprocess.Popen) -> None:
    """The child and anything it started (it leads its own session)."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_worker(ext: str, data: bytes, command: list | None = None,
               timeout: float = TIMEOUT_S) -> dict:
    """Parse one file in a fresh, limited child process and return its answer.
    `command` and `timeout` exist for the tests, which swap in a worker that
    stalls, eats memory or floods its output to prove the limits hold.

    The child's output is read up to MAX_ANSWER_BYTES and no further: an
    honest answer is at most worker.MAX_TEXT_CHARS of text, escaped, and a
    child that writes more is killed, so even a parser taken over by a file
    cannot fill this process's memory."""
    posix = os.name == "posix"
    proc = subprocess.Popen(
        (command or WORKER_COMMAND) + [ext],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=CHILD_ENV, close_fds=True,
        preexec_fn=_limit_child if posix else None,
        start_new_session=posix)
    out, err = bytearray(), bytearray()
    flooded = threading.Event()

    def feed():
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except OSError:
            pass

    def drain_out():
        while chunk := proc.stdout.read(65536):
            if len(out) + len(chunk) > MAX_ANSWER_BYTES:
                flooded.set()
                _kill_group(proc)
                return
            out.extend(chunk)

    def drain_err():
        while chunk := proc.stderr.read(65536):
            err.extend(chunk)
            del err[:-8192]               # only the tail is ever read

    threads = [threading.Thread(target=t, daemon=True) for t in (feed, drain_out, drain_err)]
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
    # Always: whatever the child left running goes too, so no leftover can
    # hold its pipes open (or keep spinning) after the answer.
    _kill_group(proc)
    proc.wait()
    for t in threads:
        t.join(timeout=5)
    if timed_out:
        return {"ok": False, "kind": "timeout", "detail": f"killed at {timeout}s"}
    if flooded.is_set():
        return {"ok": False, "kind": "unreadable", "detail": "worker answer over the cap"}
    if proc.returncode != 0:
        return _classify_exit(proc.returncode, bytes(err))
    try:
        got = json.loads(out)
    except ValueError:
        return {"ok": False, "kind": "unreadable", "detail": "worker answer unreadable"}
    if not isinstance(got, dict) or "ok" not in got:
        return {"ok": False, "kind": "unreadable", "detail": "worker answer malformed"}
    return got


def _refuse(kind: str, detail: str = "") -> dict:
    return {"ok": False, "kind": kind, "detail": detail}


def handle_request(rfile) -> dict | None:
    """Read one request from a file-like stream and answer it. None for a
    connection that sent nothing - a readiness probe, answered by connecting."""
    header = rfile.readline(1024)
    if not header:
        return None
    if not header.endswith(b"\n"):
        return _refuse("bad_request", "no header line")
    try:
        req = json.loads(header)
        ext, size = req["ext"], req["size"]
    except (ValueError, KeyError, TypeError):
        return _refuse("bad_request", "header unreadable")
    if req.get("v") != 1 or ext not in SUPPORTED or not isinstance(size, int) or size < 0:
        return _refuse("bad_request", "header refused")
    if size > MAX_INPUT_BYTES:
        return _refuse("too_large", f"{size} bytes")
    data = rfile.read(size)
    if len(data) != size:
        return _refuse("bad_request", "short body")
    if not _slots.acquire(timeout=SLOT_WAIT_S):
        return _refuse("busy", f"no slot in {SLOT_WAIT_S}s")
    try:
        return run_worker(ext, data)
    finally:
        _slots.release()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(READ_TIMEOUT_S)
        started = time.monotonic()
        try:
            result = handle_request(self.rfile)
        except OSError as e:
            result = _refuse("bad_request", type(e).__name__)
        if result is None:
            return
        # One line per file: the outcome and its cost, never its content.
        print(json.dumps({"event": "extract", "ok": result.get("ok"),
                          "kind": result.get("kind", "text"),
                          "ms": int((time.monotonic() - started) * 1000)}), flush=True)
        try:
            self.wfile.write(json.dumps(result).encode() + b"\n")
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_server(path: str = SOCKET_PATH) -> _Server:
    """Bind the socket (replacing a stale one) and return the server, not yet
    serving. The tests run it on a thread; serve() runs it as the process."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = _Server(path, _Handler)
    # Owner only: this box's own user, and root, which connects regardless of
    # the mode - the backend runs as root. Any other user in a container that
    # mounts the volume cannot connect. (A backend moved to a non-root user
    # would need a shared group here, 0o660.)
    os.chmod(path, 0o600)
    return server


def serve(path: str = SOCKET_PATH) -> None:
    server = make_server(path)

    def _stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _stop)
    print(json.dumps({"event": "listening", "socket": path}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    serve()
