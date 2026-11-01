"""Shared extractor (app/text_extract.py) - factored out of the upload
endpoint so every ingest surface behaves identically; the contract must not
drift from what upload historically accepted.

Since 2026-11-01 PDF and docx go to the extractor service. The tests at the
bottom drive the client's real conversation (_ask_extractor, captured here
before conftest swaps in the stand-in) against a fake socket: what it sends,
and how every answer and every failure maps to the three outcomes callers
know - ExtractError, the server's fault (parser_missing), the file's fault.
"""
import json
import socket

import pytest

from app import text_extract
from app.text_extract import (ExtractError, extract_text, SUPPORTED_EXTS, TEXT_EXTS,
                              DependencyMissing, FileUnreadable, ReaderUnavailable,
                              parser_missing)
from app.text_extract import _ask_extractor as REAL_ASK


def test_text_file_decodes():
    assert extract_text("notes.md", "hello world".encode()) == "hello world"


def test_historical_ext_list_unchanged():
    # Factoring must not silently widen the API's accepted types.
    assert set(TEXT_EXTS) == {"txt", "md", "py", "js", "ts", "json",
                              "yaml", "yml"}


def test_unsupported_type_flagged():
    with pytest.raises(ExtractError) as ei:
        extract_text("photo.png", b"\x89PNG")
    assert ei.value.unsupported is True


def test_empty_content_rejected_not_unsupported():
    with pytest.raises(ExtractError) as ei:
        extract_text("empty.txt", b"   \n  ")
    assert ei.value.unsupported is False


def test_supported_exts_names_only_types_extract_text_handles():
    # SUPPORTED_EXTS is what the upload picker is pinned to (2026-10-29), so
    # every type it names must reach a handler, never the unsupported refusal.
    for ext in SUPPORTED_EXTS:
        try:
            extract_text(f"f.{ext}", b"x")
        except ExtractError as e:
            assert not e.unsupported, ext
        except Exception:
            pass  # a handled type's parser refusing b"x" is not "unsupported"


def test_no_extension_unsupported():
    with pytest.raises(ExtractError) as ei:
        extract_text("Makefile", b"all: build")
    assert ei.value.unsupported is True


# -- The conversation with the extractor service (2026-11-01) -----------------

class _FakeSocket:
    """One connection: records what was sent, plays back `reply`, or raises
    `fail` at the step it names."""

    def __init__(self, reply: bytes = b"", fail: dict | None = None):
        self.reply, self.fail, self.sent, self.closed = reply, fail or {}, b"", False
        self.connected_to = None

    def settimeout(self, t):
        self.timeout = t

    def connect(self, path):
        if "connect" in self.fail:
            raise self.fail["connect"]
        self.connected_to = path

    def sendall(self, data):
        if "send" in self.fail:
            raise self.fail["send"]
        self.sent += data

    def makefile(self, mode):
        import io
        if "read" in self.fail:
            class _Raising:
                def readline(_, n):
                    raise self.fail["read"]
            return _Raising()
        return io.BytesIO(self.reply)

    def close(self):
        self.closed = True


@pytest.fixture
def wire(monkeypatch):
    """Route the client's next socket to a _FakeSocket; returns a setter."""
    made: list[_FakeSocket] = []
    monkeypatch.setattr(text_extract, "_ask_extractor", REAL_ASK)
    monkeypatch.setattr(text_extract.socket, "AF_UNIX", 1, raising=False)

    def use(reply=b"", fail=None):
        fake = _FakeSocket(reply, fail)
        made.append(fake)
        monkeypatch.setattr(text_extract.socket, "socket", lambda *a, **k: fake)
        return fake
    return use


def _line(obj) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def test_the_client_sends_one_header_line_then_the_bytes(wire):
    fake = wire(_line({"ok": True, "text": "Refunds within thirty days", "truncated": False}))
    assert extract_text("policy.pdf", b"%PDF-1.4 body") == "Refunds within thirty days"
    header, _, body = fake.sent.partition(b"\n")
    assert json.loads(header) == {"v": 1, "ext": "pdf", "size": len(b"%PDF-1.4 body")}
    assert body == b"%PDF-1.4 body"
    assert fake.connected_to == text_extract.EXTRACTOR_SOCKET and fake.closed
    assert fake.timeout == text_extract._CLIENT_TIMEOUT_S


@pytest.mark.parametrize("kind, raised, servers_fault", [
    ("empty", ExtractError, False),
    ("timeout", FileUnreadable, False),
    ("memory", FileUnreadable, False),
    ("too_large", FileUnreadable, False),
    ("unreadable", FileUnreadable, False),
    ("dependency_missing", DependencyMissing, True),
    ("parser_missing", ReaderUnavailable, True),
    ("busy", ReaderUnavailable, True),
    ("bad_request", ReaderUnavailable, True),
    ("a-kind-from-a-newer-service", ReaderUnavailable, True),
])
def test_each_answer_maps_to_the_outcome_callers_know(wire, kind, raised, servers_fault):
    wire(_line({"ok": False, "kind": kind, "detail": "PdfReadError: EOF marker not found"}))
    with pytest.raises(raised) as ei:
        extract_text("policy.pdf", b"%PDF-1.4 body")
    assert parser_missing(ei.value) is servers_fault
    if raised is ExtractError:
        assert ei.value.unsupported is False
    else:
        assert len(str(ei.value)) <= 400


@pytest.mark.parametrize("fail, reply", [
    ({"connect": FileNotFoundError("no socket")}, b""),
    ({"connect": ConnectionRefusedError()}, b""),
    ({"send": BrokenPipeError()}, b""),
    ({"read": socket.timeout()}, b""),
    (None, b'{"ok": true, "text": "cut sh'),            # no newline: cut short
    (None, b"not json\n"),
    (None, b'["a", "list"]\n'),
    (None, b'{"ok": true, "text": 42}\n'),
])
def test_every_failed_conversation_is_the_servers_fault(wire, fail, reply):
    wire(reply, fail)
    with pytest.raises(ReaderUnavailable) as ei:
        extract_text("policy.docx", b"PK\x03\x04")
    assert parser_missing(ei.value)


def test_a_platform_without_unix_sockets_is_the_servers_fault(monkeypatch):
    monkeypatch.setattr(text_extract, "_ask_extractor", REAL_ASK)
    monkeypatch.delattr(text_extract.socket, "AF_UNIX", raising=False)
    with pytest.raises(ReaderUnavailable):
        extract_text("policy.pdf", b"%PDF-1.4")
