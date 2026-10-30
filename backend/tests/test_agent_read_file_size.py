"""read_file checks a document's size against the upload limit before reading
it (2026-10-29).

read_file read a .pdf or .docx whole (p.read_bytes()) and parsed it before
anything bounded it; READ_FILE_MAX_CHARS cuts only the text that comes back,
so a very large document in the workspace cost its full read and parse on
every call. The upload route already bounds a document at MAX_UPLOAD_MB.

What these pin: a document over routers/kb.py's MAX_UPLOAD_MB is refused with
a fixed sentence on a size stat alone - never read, never parsed. CONTROL: a
document under the limit, and one exactly at it (the upload's own comparison
lets that through), is extracted as before.
"""
import pathlib

from app import agent
from app.routers import kb
from tests.test_agent_tool_results_budget import _ws
from tests.test_knowledge_sync_reads_through_extractor import WORDS, _docx, _pdf

MB = 1024 * 1024


def _read(path):
    return agent.execute_tool("read_file", {"path": path})


def _never_read(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a document over the limit was read")
    monkeypatch.setattr(pathlib.Path, "read_bytes", _boom)
    monkeypatch.setattr(agent, "extract_text", _boom)


# -- FIX ----------------------------------------------------------------------

def test_a_document_over_the_limit_is_refused_unread(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    monkeypatch.setattr(kb, "MAX_UPLOAD_MB", 1)
    (ws / "huge.pdf").write_bytes(_pdf(WORDS) + b"\n%" + b"0" * MB)
    (ws / "huge.docx").write_bytes(b"0" * (MB + 1))
    _never_read(monkeypatch)
    for name in ("huge.pdf", "huge.docx"):
        assert _read(name) == (f"Error: '{name}' is over the 1 MB document limit; "
                               f"read_file does not read it."), name


# -- CONTROL ------------------------------------------------------------------

def test_a_document_under_the_limit_is_extracted_as_before(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    monkeypatch.setattr(kb, "MAX_UPLOAD_MB", 1)
    (ws / "policy.pdf").write_bytes(_pdf(WORDS))
    (ws / "policy.docx").write_bytes(_docx(WORDS))
    for name in ("policy.pdf", "policy.docx"):
        out = _read(name)
        assert WORDS in out and "%PDF" not in out, out[:300]


def test_a_document_exactly_at_the_limit_is_read(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    monkeypatch.setattr(kb, "MAX_UPLOAD_MB", 1)
    (ws / "edge.pdf").write_bytes(b"0" * MB)
    monkeypatch.setattr(agent, "extract_text", lambda name, data: f"{len(data)} bytes read")
    assert f"{MB} bytes read" in _read("edge.pdf")
