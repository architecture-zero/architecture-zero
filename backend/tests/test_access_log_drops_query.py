"""uvicorn's access log drops a request's query string (2026-11-16).

uvicorn logs every request's path WITH its query string, and a query string
can carry what no log should keep (a question asked by URL, an authorization
code). app/access_log.py cuts it off on the access line and the WebSocket
handshake lines, and app.main installs it at import.

The first two tests start a real uvicorn with the flags the image starts the
backend with (the Dockerfile's CMD, or the entrypoint script it runs), on a
probe app that installs the filter the way app.main does, and read the line
uvicorn actually wrote:

FIX      the access line keeps the client, method, path and status, and not the
         query string
CONTROL  the same probe without the install writes the query string - so the
         probe reads the real line, and the FIX is the filter's doing
FIX      after `import app.main` each logger carries the filter exactly once,
         and installing again adds none
FIX      a WebSocket handshake line loses its query string too
CONTROL  an argument that is not a path keeps its "?"
"""
import http.client
import importlib
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CANARY = "canary-qs-7f3a1c"

PROBE_APP = '''
{install}
async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({{"type": "http.response.start", "status": 200, "headers": []}})
    await send({{"type": "http.response.body", "body": b"ok"}})
'''
# What app.main runs at import.
INSTALL = "from app import access_log\naccess_log.install()\n"


def _shipped_flags():
    """The uvicorn flags the image starts the backend with, minus the bind
    address: the Dockerfile's CMD, or - where the CMD runs an entrypoint
    script - that script's exec line."""
    cmd = None
    for line in (BACKEND / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("CMD "):
            cmd = json.loads(line[4:])
    if cmd and cmd[0] != "uvicorn":
        script = BACKEND / Path(cmd[0]).name
        for line in script.read_text(encoding="utf-8").splitlines():
            if line.startswith("exec uvicorn "):
                cmd = shlex.split(line)[1:]
    assert cmd and cmd[:2] == ["uvicorn", "app.main:app"], cmd
    flags, rest = [], iter(cmd[2:])
    for arg in rest:
        if arg in ("--host", "--port"):
            next(rest)
            continue
        flags.append(arg)
    return flags


def _served_log(work: Path, install: bool) -> str:
    """Start uvicorn on the probe, ask it GET /probe?q=CANARY once, and return
    everything the server wrote."""
    work.mkdir()
    (work / "probe_app.py").write_text(PROBE_APP.format(install=INSTALL if install else ""),
                                       encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, PYTHONUNBUFFERED="1",
               PYTHONPATH=os.pathsep.join(p for p in (str(BACKEND), os.environ.get("PYTHONPATH"))
                                          if p))
    out = work / "server.log"
    with out.open("wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "probe_app:app", "--host", "127.0.0.1",
             "--port", str(port), *_shipped_flags()],
            cwd=str(work), stdout=sink, stderr=subprocess.STDOUT, env=env)
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", f"/probe?q={CANARY}&n=8")
            assert conn.getresponse().status == 200
            conn.close()
            deadline = time.time() + 10
            while time.time() < deadline and "/probe" not in out.read_text("utf-8", "replace"):
                time.sleep(0.05)
        finally:
            proc.terminate()
            proc.wait(timeout=15)
    return out.read_text("utf-8", "replace")


def _probe_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if '"GET /probe' in ln]


def test_fix_the_access_line_keeps_the_path_and_drops_the_query(tmp_path):
    """FIX: the line the shipped server writes names the client, the method,
    the path and the status, and nothing that followed the "?"."""
    text = _served_log(tmp_path / "with", install=True)
    lines = _probe_lines(text)
    assert len(lines) == 1, text
    assert "127.0.0.1:" in lines[0] and '"GET /probe HTTP/1.1" 200' in lines[0], lines
    assert CANARY not in text and "?" not in lines[0], text


def test_control_without_the_install_the_line_carries_the_query(tmp_path):
    """CONTROL: the same probe, the install left out - uvicorn writes the
    query string, so the FIX above reads the real line."""
    lines = _probe_lines(_served_log(tmp_path / "without", install=False))
    assert len(lines) == 1, lines
    assert f'"GET /probe?q={CANARY}&n=8 HTTP/1.1" 200' in lines[0], lines


def _marked(name):
    return [f for f in logging.getLogger(name).filters if getattr(f, "_drops_query_strings", False)]


def test_fix_app_main_installs_the_filter_once_on_each_logger():
    """FIX: importing the app installs the filter on the access logger and on
    the logger the WebSocket lines use - once, however often it runs."""
    import app.main  # noqa: F401 - the import is what installs it
    from app import access_log
    assert set(access_log.LOGGERS) == {"uvicorn.access", "uvicorn.error"}
    for name in access_log.LOGGERS:
        assert len(_marked(name)) == 1, name
    access_log.install()
    importlib.reload(access_log).install()
    for name in access_log.LOGGERS:
        assert len(_marked(name)) == 1, name


def _emit(name, msg, *args):
    """What one record on logger `name` formats to, with the filter on where
    the code has one (the CONTROL below runs on the code before it too)."""
    try:
        from app import access_log
    except ImportError:
        access_log = None
    if access_log is not None:
        access_log.install()
    got = []

    class Grab(logging.Handler):
        def emit(self, record):
            got.append(record.getMessage())

    logger = logging.getLogger(name)
    level, grab = logger.level, Grab()
    logger.setLevel(logging.INFO)
    logger.addHandler(grab)
    try:
        logger.info(msg, *args)
    finally:
        logger.removeHandler(grab)
        logger.setLevel(level)
    return got


def test_fix_a_websocket_handshake_line_drops_its_query():
    """FIX: the handshake line uvicorn writes on uvicorn.error, in its own
    format (protocols/websockets/websockets_impl.py)."""
    got = _emit("uvicorn.error", '%s - "WebSocket %s" [accepted]',
                ("127.0.0.1", 50000), f"/ws?token={CANARY}")
    assert got == ["('127.0.0.1', 50000) - \"WebSocket /ws\" [accepted]"]


def test_control_an_argument_that_is_not_a_path_keeps_its_question_mark():
    """CONTROL: only a path-shaped argument is cut - any other line on the
    logger reads as it did."""
    assert _emit("uvicorn.error", "%s said %s", "who?", "what?") == ["who? said what?"]
