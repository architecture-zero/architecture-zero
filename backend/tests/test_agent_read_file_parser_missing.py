"""read_file reports a missing parser as the server's limit, not the file's
fault (2026-10-29).

read_file answered every exception out of the extractor with "no text could
be extracted from '<path>'", so on a build without pypdf (or python-docx, or
a library pypdf needs for a stream it found - its DependencyError) the agent
told the model the document was broken or empty, when the server could not
read the type at all.

What these pin: a missing parser (text_extract.parser_missing) answers the
upload route's own fixed sentence, naming the type, as the tool's error -
never the library's words, which go to the log, bounded. CONTROL: a corrupt
PDF and a textless one still answer the file's sentence.
"""
import pytest

from app import agent, text_extract
from tests.test_knowledge_sync_reads_through_extractor import WORDS, _pdf
from tests.test_missing_parser_is_not_textless import CASES, LIBRARY_WORDS


def _ws(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(agent, "AGENT_WORKSPACE", ws.resolve())
    monkeypatch.setattr(agent, "ENABLE_AGENT_TOOLS", True)
    monkeypatch.setattr(agent, "ENABLE_READ_TOOLS", True)
    return ws


def _read(path):
    return agent.execute_tool("read_file", {"path": path})


# -- FIX ----------------------------------------------------------------------

@pytest.mark.parametrize("name,body,missing,library", CASES)
def test_a_missing_parser_is_the_servers_sentence(
        monkeypatch, tmp_path, name, body, missing, library):
    ws = _ws(monkeypatch, tmp_path)
    (ws / name).write_bytes(body())
    missing(monkeypatch)
    logged = []
    monkeypatch.setattr(agent, "log_error",
                        lambda event, **f: logged.append((event, f)))
    ext = name.rsplit(".", 1)[-1]
    out = _read(name)
    assert out == "Error: " + text_extract.PARSER_MISSING_DETAIL.format(ext=ext), out
    for word in LIBRARY_WORDS:
        assert word not in out, (word, out)
    assert len(logged) == 1, logged
    event, fields = logged[0]
    assert event == "agent_read_file_parser_missing"
    assert fields["ext"] == ext
    assert library in fields["error"] and len(fields["error"]) <= 300


# -- CONTROL ------------------------------------------------------------------

def test_a_corrupt_or_textless_pdf_is_still_the_files_sentence(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    (ws / "scan.pdf").write_bytes(b"%PDF-1.4\nnot a pdf at all\n" + b"x" * 600)
    (ws / "blank.pdf").write_bytes(_pdf(None))
    for name in ("scan.pdf", "blank.pdf"):
        assert _read(name) == f"Error: no text could be extracted from '{name}'."


def test_a_readable_pdf_is_read_as_before(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path)
    (ws / "policy.pdf").write_bytes(_pdf(WORDS))
    assert WORDS in _read("policy.pdf")
