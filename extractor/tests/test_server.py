"""The box's limits and wire, on Linux, where it runs (server.py).

Each file is parsed in a fresh child process; these prove the child is held
to its clock, its memory cap and no file writes, with a worker swapped in
that stalls, eats memory or writes a file. Then the wire: requests read and
refused as the format says, one file at a time, and the backend's own client
(backend/app/text_extract.py, loaded by path) talking to a real server over
a real socket - so the two halves cannot drift apart unnoticed.
"""
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time

import pytest

if os.name != "posix":
    pytest.skip("the box runs on Linux (Unix sockets, rlimits)", allow_module_level=True)

import server  # noqa: E402
from sample_files import bomb_pdf, pdf  # noqa: E402

WORDS = "Refunds go to the original card within thirty days"
PY = [sys.executable, "-I", "-c"]


def _command(code: str) -> list:
    """A worker that runs `code` instead of parsing (run_worker appends the ext)."""
    return PY + [code]


# -- The limits around each parse -------------------------------------------------

def test_a_real_pdf_parses_in_a_limited_child():
    got = server.run_worker("pdf", pdf([WORDS]))
    assert got["ok"] is True and WORDS in got["text"], got


def test_a_stalled_parse_is_killed_at_the_clock():
    started = time.monotonic()
    got = server.run_worker("pdf", b"", command=_command("import time; time.sleep(120)"),
                            timeout=2)
    assert got["kind"] == "timeout", got
    assert time.monotonic() - started < 10


def test_a_spinning_parse_is_killed_at_the_clock():
    """A busy loop, the shape of the parser bugs this exists for."""
    got = server.run_worker("pdf", b"", command=_command("while True: pass"), timeout=2)
    assert got["kind"] == "timeout", got


def test_a_parse_that_eats_memory_is_stopped_at_the_cap():
    got = server.run_worker("pdf", b"", command=_command(
        "b = bytearray(1024 * 1024 * 1024)\nprint('{\"ok\": true, \"text\": \"x\"}')"))
    assert got["ok"] is False and got["kind"] == "memory", got


def test_the_child_writes_no_file():
    """Its file-size limit is zero: not one byte reaches a file. (Python
    ignores SIGXFSZ, so the write raises EFBIG; the flush makes it surface.)"""
    path = os.path.join(tempfile.gettempdir(), "extractor-write-probe")
    got = server.run_worker("pdf", b"", command=_command(
        f"f = open({path!r}, 'w'); f.write('x'); f.flush()\n"
        "print('{\"ok\": true, \"text\": \"wrote\"}')"))
    assert got["ok"] is False, got
    assert not os.path.exists(path) or os.path.getsize(path) == 0


def test_a_child_that_floods_its_output_is_cut_off(monkeypatch):
    """Even a parser taken over by a file cannot fill the server's memory."""
    monkeypatch.setattr(server, "MAX_ANSWER_BYTES", 1024 * 1024)
    started = time.monotonic()
    got = server.run_worker("pdf", b"", command=_command(
        "import sys\nwhile True: sys.stdout.write('x' * 65536)"), timeout=10)
    assert got == {"ok": False, "kind": "unreadable", "detail": "worker answer over the cap"}, got
    assert time.monotonic() - started < 5


def test_nothing_the_child_started_outlives_its_answer():
    """A grandchild that would keep spinning, or hold the pipe open, is
    killed with the child's group as soon as the answer is in."""
    code = ("import json, subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(json.dumps({'ok': True, 'text': str(p.pid)}))")
    got = server.run_worker("pdf", b"", command=PY + [code], timeout=10)
    pid = int(got["text"])
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    if alive:      # a zombie still answers kill(0); its state says it is dead
        with open(f"/proc/{pid}/stat") as f:
            alive = f.read().split()[2] != "Z"
    assert not alive, f"grandchild {pid} outlived the answer"


def test_the_child_sees_none_of_the_servers_environment(monkeypatch):
    monkeypatch.setenv("SOME_SECRET_KEY", "must-not-reach-the-child")
    got = server.run_worker("pdf", b"", command=_command(
        "import json, os; print(json.dumps({'ok': True, 'text': json.dumps(sorted(os.environ))}))"))
    seen = set(json.loads(got["text"]))
    assert "SOME_SECRET_KEY" not in seen
    assert seen <= set(server.CHILD_ENV) | {"LC_CTYPE"}, seen


def test_a_decompression_bomb_is_contained():
    """A small file that inflates to 600 MB: pypdf's own limit or the memory
    cap stops it, quickly, and the server is unharmed."""
    started = time.monotonic()
    got = server.run_worker("pdf", bomb_pdf(600 * 1024 * 1024))
    assert got["ok"] is False or len(got.get("text", "")) < 1000, str(got)[:300]
    assert time.monotonic() - started < server.TIMEOUT_S + 5
    assert server.run_worker("pdf", pdf([WORDS]))["ok"] is True


# -- The wire ---------------------------------------------------------------------

def _request(ext: str, data: bytes, size: int | None = None, v: int = 1) -> io.BytesIO:
    header = json.dumps({"v": v, "ext": ext, "size": len(data) if size is None else size})
    return io.BytesIO(header.encode() + b"\n" + data)


def test_a_request_is_answered():
    got = server.handle_request(_request("pdf", pdf([WORDS])))
    assert got["ok"] is True and WORDS in got["text"]


def test_a_connection_that_sends_nothing_is_a_probe_and_gets_no_answer(monkeypatch):
    monkeypatch.setattr(server, "run_worker", lambda *a, **k: pytest.fail("parsed"))
    assert server.handle_request(io.BytesIO(b"")) is None


@pytest.mark.parametrize("stream, kind", [
    (io.BytesIO(b"no newline"), "bad_request"),
    (io.BytesIO(b"not json\n"), "bad_request"),
    (_request("exe", b"MZ"), "bad_request"),
    (_request("pdf", b"%PDF", v=2), "bad_request"),
    (_request("pdf", b"%PDF", size=100), "bad_request"),           # short body
    (_request("pdf", b"", size=server.MAX_INPUT_BYTES + 1), "too_large"),
])
def test_bad_requests_are_refused_unparsed(stream, kind, monkeypatch):
    monkeypatch.setattr(server, "run_worker", lambda *a, **k: pytest.fail("parsed"))
    assert server.handle_request(stream)["kind"] == kind


def test_one_file_at_a_time_and_a_waiting_one_is_told_busy(monkeypatch):
    monkeypatch.setattr(server, "SLOT_WAIT_S", 0.2)
    assert server._slots.acquire(timeout=1)
    try:
        assert server.handle_request(_request("pdf", pdf([WORDS])))["kind"] == "busy"
    finally:
        server._slots.release()


def _load_backend_client():
    path = pathlib.Path(__file__).resolve().parents[2] / "backend" / "app" / "text_extract.py"
    if not path.exists():
        pytest.skip("the backend's client is not beside this checkout")
    spec = importlib.util.spec_from_file_location("backend_text_extract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def live(tmp_path):
    """A real server on a real socket, and the backend's client pointed at it."""
    sock = str(tmp_path / "extractor.sock")
    srv = server.make_server(sock)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    client = _load_backend_client()
    client.EXTRACTOR_SOCKET = sock
    yield client, srv
    srv.shutdown()
    srv.server_close()


def test_the_backend_client_and_the_server_agree_end_to_end(live):
    client, _ = live
    assert WORDS in client.extract_text("policy.pdf", pdf([WORDS]))
    with pytest.raises(client.ExtractError):
        client.extract_text("blank.pdf", pdf([None]))
    with pytest.raises(client.FileUnreadable):
        client.extract_text("scan.pdf", b"%PDF-1.4\nnot a pdf at all\n")


def test_the_backend_client_reads_a_stopped_server_as_the_servers_fault(live):
    client, srv = live
    srv.shutdown()
    srv.server_close()
    os.unlink(client.EXTRACTOR_SOCKET)
    with pytest.raises(client.ReaderUnavailable) as ei:
        client.extract_text("policy.pdf", pdf([WORDS]))
    assert client.parser_missing(ei.value)
