"""The agent's read_file returns a document's text, refuses other binaries and
caps what it returns (2026-10-29).

read_file returned p.read_text(encoding="utf-8", errors="replace") for every
path: a PDF or docx in the workspace came back as its bytes decoded ("%PDF-1.4
... endobj", a docx's zip), an image the same, and any file came back whole -
nothing between the tool and the model's context capped it (the chat loop and
the eval runner append the result as it is).

What these pin: a PDF or docx is returned as text_extract.extract_text's text;
a file the extractor cannot read is a fixed sentence, never the library's
words; a binary that is not a document is refused unread; what is returned is
cut at READ_FILE_MAX_CHARS with a marker. The CONTROL cases pass on the code
before the change too: a small .md - CRLF line ends and a stray non-UTF-8 byte -
comes back exactly as read_text gave it, a path outside the workspace and a
file above the caller's level are refused as before, the clearance check
running before any extraction.
"""
from app import agent
from app.permissions import MEMBER_LEVEL
from app.runtime_config import MAX_CONTEXT_TOKENS
from tests.test_knowledge_sync_reads_through_extractor import WORDS, _docx, _pdf

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89" + b"\x00" * 64)


def _ws(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())
    monkeypatch.setattr(agent, "ENABLE_AGENT_TOOLS", True)
    monkeypatch.setattr(agent, "ENABLE_READ_TOOLS", True)
    return ws


def _read(path, **kw):
    return agent.execute_tool("read_file", {"path": path}, **kw)


# -- FIX ----------------------------------------------------------------------

def test_a_pdf_comes_back_as_its_text(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    (ws / "policy.pdf").write_bytes(_pdf(WORDS))
    out = _read("policy.pdf")
    assert WORDS in out, out[:200]
    assert "%PDF" not in out and "endobj" not in out, out[:200]


def test_a_docx_comes_back_as_its_text(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    (ws / "policy.docx").write_bytes(_docx(WORDS))
    out = _read("policy.docx")
    assert out.strip() == WORDS, out[:200]


def test_a_document_with_no_text_is_a_fixed_sentence(monkeypatch, tmp_path):
    """A broken PDF and a textless one: one sentence naming the path the
    caller gave, never pypdf's words and never the bytes."""
    ws = _ws(monkeypatch, tmp_path)
    (ws / "scan.pdf").write_bytes(b"%PDF-1.4\nnot a pdf at all\n" + b"x" * 600)
    (ws / "blank.pdf").write_bytes(_pdf(None))
    for name in ("scan.pdf", "blank.pdf"):
        out = _read(name)
        assert out == f"Error: no text could be extracted from '{name}'.", out[:200]


def test_a_binary_that_is_not_a_document_is_refused_unread(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    (ws / "logo.png").write_bytes(PNG)
    out = _read("logo.png")
    assert out == ("Error: 'logo.png' is a binary file; read_file returns text "
                   "files and documents (.docx, .pdf) only."), out[:200]


def test_a_file_over_the_bound_is_cut_with_a_marker(monkeypatch, tmp_path):
    """The bound is the chat's history budget at the chat's own estimate of
    four characters a token: one read never carries more than the whole
    conversation the chat budgets for."""
    ws = _ws(monkeypatch, tmp_path)
    cap = MAX_CONTEXT_TOKENS * 4
    body = "".join(f"line {i:06d}\n" for i in range(cap // 12 + 500))
    assert len(body) > cap
    (ws / "big.log").write_text(body, encoding="utf-8")
    out = _read("big.log")
    assert out.startswith(body[:cap]), out[:80]
    assert out[cap:] == (f"\n\n[read_file: cut at {cap} characters - the file "
                         f"is longer and the rest was not returned]"), out[cap:]


def test_an_extracted_document_is_cut_at_the_same_bound(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "READ_FILE_MAX_CHARS", 10)
    (ws / "policy.docx").write_bytes(_docx(WORDS))
    out = _read("policy.docx")
    assert out.startswith(WORDS[:10] + "\n\n[read_file: cut at 10 characters"), out


def test_a_file_at_the_bound_is_whole(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "READ_FILE_MAX_CHARS", 10, raising=False)
    (ws / "ten.txt").write_text("0123456789", encoding="utf-8")
    assert _read("ten.txt") == "0123456789"


# -- CONTROL ------------------------------------------------------------------

def test_a_small_md_comes_back_exactly_as_before(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    p = ws / "notes.md"
    p.write_bytes(b"# Notes\r\nline one\r\nstray \xff byte\r\n")
    assert _read("notes.md") == p.read_text(encoding="utf-8", errors="replace")


def test_a_path_outside_the_workspace_is_refused_as_before(monkeypatch, tmp_path):
    _ws(monkeypatch, tmp_path)
    (tmp_path / "outside.pdf").write_bytes(_pdf(WORDS))
    out = _read("../outside.pdf")
    assert out == "Permission denied: Path '../outside.pdf' escapes the agent workspace"


def test_clearance_is_checked_before_any_extraction(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    (ws / "policy.pdf").write_bytes(_pdf(WORDS))
    monkeypatch.setattr(agent, "_may_read", lambda p, level: level is None)
    import app.text_extract as te

    def _boom(*a, **k):
        raise AssertionError("extracted before the clearance check")
    monkeypatch.setattr(te, "extract_text", _boom)
    # raising=False: the code before the change has no such name, and this
    # CONTROL passes there too.
    monkeypatch.setattr(agent, "extract_text", _boom, raising=False)
    out = _read("policy.pdf", user_level=MEMBER_LEVEL)
    assert out == "Permission denied: 'policy.pdf' is above your access level."
