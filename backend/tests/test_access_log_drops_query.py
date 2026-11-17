"""uvicorn's access log drops a request's query string (2026-11-16).

uvicorn logs every request's path WITH its query string, and a query string
can carry what no log should keep (a question asked by URL, an authorization
code). app/access_log.py cuts it off on the access line and the WebSocket
handshake lines, and app.main installs it at import.

The first two tests start a real uvicorn with the flags the image starts the
backend with (the Dockerfile's CMD, or the entrypoint script it runs), on a
probe app that installs the filter the way app.main does, send it raw request
lines - the origin form, and the targets the security read of 2026-11-16
showed keeping their query under a leading-slash rule: the absolute form, a
target that starts with "?", one with no slash, `OPTIONS *?...`, and a
WebSocket handshake with an absolute target - and read the lines uvicorn
actually wrote:

FIX      every access line keeps the client, method, target and status, and
         none keeps the query string; the WebSocket line neither
CONTROL  the same probe without the install writes the query string on every
         one of those lines - so the probe reads the real lines, and the FIX
         is the filter's doing
FIX      after `import app.main` each logger carries the filter exactly once,
         and installing again adds none
FIX      a WebSocket handshake line loses its query string, the target's first
         character whatever it is
FIX      on the access logger every argument is cut at its "?"
CONTROL  on the error logger an argument that is not a path keeps its "?"
CONTROL  an odd record is kept and never raises
"""
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
    if scope["type"] == "websocket":
        await receive()
        await send({{"type": "websocket.close", "code": 1008}})
        return
    if scope["type"] != "http":
        return
    await send({{"type": "http.response.start", "status": 200, "headers": []}})
    await send({{"type": "http.response.body", "body": b"ok"}})
'''
# What app.main runs at import.
INSTALL = "from app import access_log\naccess_log.install()\n"

_HEAD = b"Host: h\r\nConnection: close\r\n\r\n"
_Q = b"q=" + CANARY.encode() + b"&n=8"
# (request bytes, the line the filter must leave)
REQUESTS = [
    (b"GET /probe?" + _Q + b" HTTP/1.1\r\n" + _HEAD, '"GET /probe HTTP/1.1" 200'),
    (b"GET http://h/probe?" + _Q + b" HTTP/1.1\r\n" + _HEAD, '"GET http%3A//h/probe HTTP/1.1" 200'),
    (b"GET ?" + _Q + b" HTTP/1.1\r\n" + _HEAD, '"GET  HTTP/1.1" 200'),
    (b"GET probe?" + _Q + b" HTTP/1.1\r\n" + _HEAD, '"GET probe HTTP/1.1" 200'),
    (b"OPTIONS *?" + _Q + b" HTTP/1.1\r\n" + _HEAD, '"OPTIONS %2A HTTP/1.1" 200'),
    (b"GET http://h/ws?" + _Q + b" HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n"
     b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
     b"Sec-WebSocket-Version: 13\r\n\r\n", '"WebSocket http%3A//h/ws" 403'),
]


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


def _status(port: int, request: bytes) -> str:
    """Send one raw request and return the response's status line."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    data = b""
    try:
        s.sendall(request)
        while b"\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    return data.split(b"\r\n", 1)[0].decode("latin-1")


def _served_log(work: Path, install: bool) -> str:
    """Start uvicorn on the probe, send it every request in REQUESTS, and
    return everything the server wrote."""
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
            statuses = [_status(port, request) for request, _line in REQUESTS]
            assert statuses[:5] == ["HTTP/1.1 200 OK"] * 5, statuses
            assert statuses[5].startswith("HTTP/1.1 403"), statuses
            deadline = time.time() + 10
            while time.time() < deadline and "WebSocket" not in out.read_text("utf-8", "replace"):
                time.sleep(0.05)
        finally:
            proc.terminate()
            proc.wait(timeout=15)
    return out.read_text("utf-8", "replace")


def _request_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if " HTTP/1.1\" " in ln or '"WebSocket ' in ln]


def test_fix_every_access_line_keeps_the_target_and_drops_the_query(tmp_path):
    """FIX: each line the shipped server writes names the client, the method,
    the target and the status, and nothing that followed the "?" - whatever
    the target's first character, and on the WebSocket handshake line too."""
    text = _served_log(tmp_path / "with", install=True)
    lines = _request_lines(text)
    assert len(lines) == len(REQUESTS), text
    for line, (_request, kept) in zip(lines, REQUESTS):
        assert kept in line, (kept, line)
    assert all("127.0.0.1" in line for line in lines), lines
    assert CANARY not in text, text


def test_control_without_the_install_every_line_carries_the_query(tmp_path):
    """CONTROL: the same probe, the install left out - uvicorn writes the
    query string on every one of those lines, so the FIX above reads the real
    lines."""
    lines = _request_lines(_served_log(tmp_path / "without", install=False))
    assert len(lines) == len(REQUESTS), lines
    assert all(CANARY in line for line in lines), lines


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
    the code has one (the CONTROLs below run on the code before it too)."""
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
    """FIX: the handshake lines uvicorn writes on uvicorn.error, in their own
    format (protocols/websockets/websockets_impl.py), with a path target and
    with an absolute one."""
    got = _emit("uvicorn.error", '%s - "WebSocket %s" [accepted]',
                ("127.0.0.1", 50000), f"/ws?token={CANARY}")
    assert got == ["('127.0.0.1', 50000) - \"WebSocket /ws\" [accepted]"]
    got = _emit("uvicorn.error", '%s - "WebSocket %s" %d',
                ("127.0.0.1", 50000), f"http%3A//h/ws?token={CANARY}", 403)
    assert got == ["('127.0.0.1', 50000) - \"WebSocket http%3A//h/ws\" 403"]


def test_fix_on_the_access_logger_every_argument_is_cut():
    """FIX: the access line's target is cut whatever its first character."""
    got = _emit("uvicorn.access", '%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:50000", "GET", f"?q={CANARY}", "1.1", 200)
    assert got == ['127.0.0.1:50000 - "GET  HTTP/1.1" 200']


def test_control_an_argument_that_is_not_a_path_keeps_its_question_mark():
    """CONTROL: on the error logger only a path-shaped argument, or a
    WebSocket line's target, is cut - any other line reads as it did."""
    assert _emit("uvicorn.error", "%s said %s", "who?", "what?") == ["who? said what?"]


class _Weird:
    def __str__(self):
        raise RuntimeError("no str")


def test_control_an_odd_record_is_kept_and_never_raises():
    """CONTROL: a record whose arguments are not what uvicorn sends - a dict,
    None, bytes, a nested tuple, an object that cannot be printed - goes
    through the filter on both loggers without an exception and is kept."""
    try:
        from app import access_log
    except ImportError:
        return
    filters = [access_log.DropQueryStrings()]
    try:
        filters.append(access_log.DropQueryStrings(every_argument=True))
    except TypeError:
        pass                                   # the filter before it had one rule
    for flt in filters:
        for msg, args in (('%s - "WebSocket %s"', (None,)), ("%s", (b"/p?q=x",)),
                          ("%s", (("x", "/p?q=x"),)), ("%s", (_Weird(),)),
                          (None, ("/p?q=x",)), ("m", ())):
            rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, args, None)
            assert flt.filter(rec) is True
        for odd in ({"a": "/p?q=x"}, ["/p?q=x"], "/p?q=x", None):
            rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "m", (), None)
            rec.args = odd
            assert flt.filter(rec) is True and rec.args is odd
