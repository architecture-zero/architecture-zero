"""The parse itself (worker.py): real PDF and Word files in, one answer out.

These run anywhere pypdf and python-docx are installed. The limits around a
parse - the clock, the memory cap, no file writes - are server.py's, and
test_server.py proves them on Linux, where the box runs.
"""
import json
import pathlib
import subprocess
import sys

import pytest

import worker
from sample_files import docx, encrypted_pdf, pdf

WORDS = "Refunds go to the original card within thirty days"
WORKER = pathlib.Path(worker.__file__)


def test_a_pdf_answers_its_text():
    got = worker.answer("pdf", pdf([WORDS]))
    assert got["ok"] is True and WORDS in got["text"] and got["truncated"] is False


def test_a_docx_answers_its_text():
    got = worker.answer("docx", docx([WORDS, "Second paragraph"]))
    assert got == {"ok": True, "text": f"{WORDS}\nSecond paragraph", "truncated": False}


def test_a_pdf_with_no_text_is_empty():
    assert worker.answer("pdf", pdf([None]))["kind"] == "empty"


@pytest.mark.parametrize("ext, data", [("pdf", b"%PDF-1.4\nnot a pdf at all\n" + b"x" * 600),
                                       ("pdf", b""), ("docx", b"PK\x03\x04 not a zip")])
def test_bytes_the_parser_cannot_read_are_unreadable_with_bounded_detail(ext, data):
    got = worker.answer(ext, data)
    assert got["ok"] is False and got["kind"] == "unreadable", got
    assert 0 < len(got["detail"]) <= 300


def test_an_unknown_type_is_unreadable_not_parsed():
    assert worker.answer("exe", b"MZ")["kind"] == "unreadable"


def test_a_missing_parser_is_parser_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    got = worker.answer("pdf", pdf([WORDS]))
    assert got["kind"] == "parser_missing" and "pypdf" in got["detail"], got


def test_a_library_pypdf_lacks_is_dependency_missing(monkeypatch):
    import pypdf
    from pypdf.errors import DependencyError

    def reader(*a, **k):
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")
    monkeypatch.setattr(pypdf, "PdfReader", reader)
    got = worker.answer("pdf", pdf([WORDS]))
    assert got["kind"] == "dependency_missing" and "cryptography" in got["detail"], got


def test_running_out_of_memory_is_memory(monkeypatch):
    def eat(ext, data):
        raise MemoryError()
    monkeypatch.setattr(worker, "_extract", eat)
    assert worker.answer("pdf", b"%PDF")["kind"] == "memory"


def test_pages_past_the_cap_are_not_read(monkeypatch):
    monkeypatch.setattr(worker, "MAX_PAGES", 2)
    got = worker.answer("pdf", pdf(["page one", "page two", "page three"]))
    assert "page one" in got["text"] and "page two" in got["text"]
    assert "page three" not in got["text"]


def test_text_past_the_cap_is_cut_and_says_so(monkeypatch):
    monkeypatch.setattr(worker, "MAX_TEXT_CHARS", 10)
    got = worker.answer("pdf", pdf([WORDS]))
    assert got["ok"] is True and len(got["text"]) == 10 and got["truncated"] is True


def test_the_script_reads_stdin_and_prints_one_json_line():
    run = subprocess.run([sys.executable, "-I", str(WORKER), "pdf"], input=pdf([WORDS]),
                         capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr[-500:]
    lines = run.stdout.splitlines()
    assert len(lines) == 1 and WORDS in json.loads(lines[0])["text"]


def test_the_box_installs_the_two_parsers_and_the_aes_provider_only():
    """cryptography is pypdf's AES provider (2026-11-01: without it an
    AES-encrypted PDF with an empty password - one the backend used to read -
    was refused). No brotli: pypdf refuses those streams (dependency_missing)
    instead of decoding them natively. check_locks.py confirms the image."""
    reqs = (WORKER.parent / "requirements.txt").read_text(encoding="utf-8").lower()
    names = {line.split("==")[0].strip() for line in reqs.splitlines() if line.strip()}
    assert names == {"pypdf", "python-docx", "cryptography"}, names


@pytest.mark.parametrize("algorithm", ["RC4-40", "RC4-128", "AES-128", "AES-256"])
def test_a_pdf_encrypted_with_an_empty_password_reads(algorithm):
    got = worker.answer("pdf", encrypted_pdf([WORDS], algorithm))
    assert got["ok"] is True and WORDS in got["text"], got


def test_a_pdf_that_needs_a_password_is_unreadable():
    got = worker.answer("pdf", encrypted_pdf([WORDS], "AES-256", user_password="open-sesame"))
    assert got["ok"] is False and got["kind"] == "unreadable", got
