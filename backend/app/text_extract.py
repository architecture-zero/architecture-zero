"""Shared text extraction for ingest surfaces.

One extractor so a file type behaves identically whichever ingest surface it
arrives by (upload endpoint today, any batch path tomorrow). Raises
ExtractError; callers decide the shape - upload maps unsupported->400 and
empty->422, a batch sync marks the file skipped and moves on. A failure that
is the server's - the reader down, busy, or built without a parser - raises
ReaderUnavailable, which parser_missing tells apart: never the file's fault,
so never "no text".

PDF AND WORD FILES ARE NEVER PARSED IN THIS PROCESS (2026-11-01). They go to
the extractor service - a locked container with no network, no secrets, no
mounts but a socket folder, a read-only filesystem, no capabilities and a
non-root user, which parses each file in a fresh child process under a clock
(20 s for a normal file, growing with its size to at most 2 minutes) and a
512 MB memory cap (extractor/server.py). This backend no longer
has pypdf or python-docx installed at all. A parser bug in either library -
pure-Python pypdf's have all been hangs and memory blow-ups so far - costs
the box one child process; it cannot reach this process, its secrets or its
data. The text types still decode here: a UTF-8 decode parses nothing.
"""
import json
import os
import socket


class ExtractError(ValueError):
    """unsupported=True means the file TYPE is not handled (vs. a handled
    type that yielded no text)."""

    def __init__(self, message: str, unsupported: bool = False):
        super().__init__(message)
        self.unsupported = unsupported


class ReaderUnavailable(RuntimeError):
    """The server could not read the file - the extractor is down, busy, or
    lacks a parser - so the file did not fail and a retry may read it. Its
    message is the reason, for a log line; a caller shows its own fixed
    sentence (PARSER_MISSING_DETAIL)."""


class DependencyMissing(ReaderUnavailable):
    """pypdf lacks a library a stream in this file needs (brotli, an AES
    provider - left out of the extractor on purpose). The server's limit, as
    a missing parser is, but tied to this one file: a caller that retries
    the server's faults may treat this one as final for the file."""


class FileUnreadable(Exception):
    """The file itself could not be read: not a valid file, or it ran past
    the extractor's clock, memory cap or size limit. The file's fault; its
    message is the reason and the parser's words, for a log line only."""


# Deliberately identical to the upload endpoint's historical list - factoring
# must not silently widen what the API accepts.
TEXT_EXTS = ("txt", "md", "py", "js", "ts", "json", "yaml", "yml")

# The types only the extractor service reads.
DOCUMENT_EXTS = ("pdf", "docx")

# Every type extract_text handles, declared once (2026-10-29): the admin
# upload picker's UPLOAD_TYPES (frontend/src/admin/KBManage.tsx) is pinned to
# this by tests/test_upload_types_pin.py, so a type added here and not to the
# picker, or the reverse, fails the suite instead of drifting by hand.
SUPPORTED_EXTS = DOCUMENT_EXTS + TEXT_EXTS

# What a caller says when the server could not read a type (2026-10-29;
# reworded 2026-11-01, when "the server's fault" came to include the
# extractor service being down): the upload's 500, the syncs' skipped entry
# and the agent's read_file error. It names the type only; the reason goes
# to the log, bounded.
PARSER_MISSING_DETAIL = ("This server cannot read .{ext} files right now: the part of it "
                         "that extracts their text is missing or not running. This is a "
                         "fault on the server, not in the file.")

# The extractor's socket, on the volume both containers mount
# (docker-compose.yml). The client waits longer than the extractor's own
# slot wait (30 s) plus its longest clock (120 s), so the extractor always
# answers first.
EXTRACTOR_SOCKET = os.getenv("EXTRACTOR_SOCKET", "/run/extractor/extractor.sock")
_CLIENT_TIMEOUT_S = 180
# The extractor's answer is one JSON line: at most 5M characters of text,
# escaped. A line past this bound is not an answer.
_MAX_ANSWER_BYTES = 64 * 1024 * 1024

# Why a file was refused, by the extractor's kind; the words go to a log.
_FILE_REASONS = {
    "timeout": "the file took longer than the reader's clock",
    "memory": "the file needed more memory than the reader's cap",
    "too_large": "the file is over the reader's size limit",
    "unreadable": "the file could not be parsed",
}


def parser_missing(e: BaseException) -> bool:
    """Whether an extraction failure is the server's (2026-10-29): the
    extractor service down, busy or without a parser (ReaderUnavailable), or
    an ImportError. Any other exception is the file's: bytes the parser could
    not read. A caller never treats this one as "the file has no text": the
    file did not fail, the server did, and a healthy server reads it."""
    return isinstance(e, (ImportError, ReaderUnavailable))


def _ask_extractor(ext: str, data: bytes) -> dict:
    """One request to the extractor service: the file in, its answer out.
    Every way the conversation can fail is the server's fault."""
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        raise ReaderUnavailable("this platform has no Unix sockets for the extractor")
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(_CLIENT_TIMEOUT_S)
    try:
        try:
            sock.connect(EXTRACTOR_SOCKET)
        except OSError as e:
            raise ReaderUnavailable(f"extractor not reachable: {type(e).__name__}") from e
        header = json.dumps({"v": 1, "ext": ext, "size": len(data)}).encode() + b"\n"
        try:
            sock.sendall(header + data)
            reply = sock.makefile("rb").readline(_MAX_ANSWER_BYTES)
        except OSError as e:
            raise ReaderUnavailable(f"extractor conversation failed: {type(e).__name__}") from e
    finally:
        sock.close()
    if not reply.endswith(b"\n"):
        raise ReaderUnavailable("extractor answer cut short")
    try:
        got = json.loads(reply)
    except ValueError as e:
        raise ReaderUnavailable("extractor answer unreadable") from e
    if not isinstance(got, dict) or "ok" not in got:
        raise ReaderUnavailable("extractor answer malformed")
    return got


def _document_text(ext: str, data: bytes) -> str:
    got = _ask_extractor(ext, data)
    if got.get("ok"):
        text = got.get("text")
        if not isinstance(text, str):
            raise ReaderUnavailable("extractor answer malformed")
        return text
    kind = str(got.get("kind", ""))[:32]
    detail = str(got.get("detail", ""))[:300]
    if kind == "empty":
        raise ExtractError("No text could be extracted from file")
    if kind == "dependency_missing":
        raise DependencyMissing(detail or kind)
    if kind in _FILE_REASONS:
        raise FileUnreadable(f"{_FILE_REASONS[kind]}: {detail}" if detail else _FILE_REASONS[kind])
    # parser_missing, busy, bad_request, or a kind this build does not know.
    raise ReaderUnavailable(f"{kind}: {detail}" if detail else kind)


def extract_text(name: str, data: bytes) -> str:
    """Text for ingestion from a filename + raw bytes. PDF and docx from the
    extractor service, the TEXT_EXTS decode as UTF-8 (errors ignored)."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in DOCUMENT_EXTS:
        text = _document_text(ext, data)
    elif ext in TEXT_EXTS:
        text = data.decode("utf-8", errors="ignore")
    else:
        raise ExtractError(f"Unsupported file type: .{ext}", unsupported=True)
    if not text.strip():
        raise ExtractError("No text could be extracted from file")
    return text
