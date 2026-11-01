"""A stand-in for the extractor service in the backend's tests (2026-11-01).

The backend has no PDF or Word parser: every such file goes to the extractor
service over a Unix socket (app/text_extract.py). The suite has no service to
talk to, so conftest installs this stand-in in its place for every test. It
answers as the service does: the text a test registered for those exact
bytes ("" registers a file that parses to no text), and for any other bytes
the service's answer to a file the parser cannot read. A test that needs the
service in another state - down, busy, built without a parser - sets
STUB.answer_with for itself; the fixture resets it.

The service's own parsing, limits and locks are tested where they live, in
extractor/tests/.
"""
import hashlib

_TEXTS: dict[str, str] = {}

# What the service says for bytes its parser cannot read, by type.
_UNREADABLE_DETAIL = {"pdf": "PdfReadError: EOF marker not found",
                      "docx": "PackageNotFoundError: Package not found"}


def register(data: bytes, text: str | None) -> bytes:
    """Teach the stand-in what these bytes hold; returns them unchanged."""
    _TEXTS[hashlib.sha256(data).hexdigest()] = text or ""
    return data


def answer(kind: str, detail: str = "") -> dict:
    """The service's refusal of a given kind (worker.py and server.py list them)."""
    return {"ok": False, "kind": kind, "detail": detail}


class Stub:
    def __init__(self):
        self.answer_with = None
        self.calls: list[tuple[str, int]] = []

    def default(self, ext: str, data: bytes) -> dict:
        text = _TEXTS.get(hashlib.sha256(data).hexdigest())
        if text is None:
            return answer("unreadable", _UNREADABLE_DETAIL.get(ext, "ValueError: unreadable"))
        if not text.strip():
            return answer("empty")
        return {"ok": True, "text": text, "truncated": False}

    def __call__(self, ext: str, data: bytes) -> dict:
        self.calls.append((ext, len(data)))
        got = self.answer_with
        if got is None:
            return self.default(ext, data)
        if callable(got):
            got = got(ext, data)
        if isinstance(got, BaseException):
            raise got
        return got


STUB = Stub()
