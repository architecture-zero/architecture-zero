"""The knowledge sync reads a watched PDF or docx through the extractor (2026-10-29).

app/ingest_sync.py watched .pdf but read every watched file with
read_text(encoding="utf-8", errors="ignore"): a PDF dropped into the knowledge
tree or docs/ was indexed as its bytes decoded ("%PDF-1.4 ... endobj"), never
its words, and .docx - a type the upload extracts - was not watched at all.

What these pin: the knowledge sync, the docs sync and the watcher index a real
PDF or docx as text_extract.extract_text's text; the watched set IS the
extractor's set; a broken or textless PDF is skipped with one bounded log line
and the sync goes on; the corpus probe reads a file by the sync's rule. The
CONTROL cases pass on the code before the change too: an existing .md - CRLF
line ends and a stray non-UTF-8 byte, the shape where the extractor's decode
and read_text differ - keeps its fingerprint and its chunk ids, and the corpus
probe parses no PDF while a text file answers.

The store is test_knowledge_orphan_prune's index fixture, real in-memory chroma
and the fake, as the sync's other store-level tests run.
"""
import hashlib
import io

import pytest

import app.database as database
import app.ingest_sync as ingest_mod
from app.chunking import chunk_markdown_sections
from app.text_extract import SUPPORTED_EXTS, extract_text
from tests.test_knowledge_orphan_prune import EMBEDDING, index  # noqa: F401

WORDS = "Refunds go to the original card within thirty days"


def _pdf(text: str | None) -> bytes:
    """A one-page PDF built by hand: Helvetica, one line of `text` (None
    gives a page with no text), a correct xref table."""
    content = (f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET" if text is not None else "").encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def _docx(text: str) -> bytes:
    import docx
    doc = docx.Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _cid(text: str, name: str, dept: str = "general") -> str:
    return hashlib.md5(f"{dept}::{name}::{text}".encode(),
                       usedforsecurity=False).hexdigest()


@pytest.fixture
def tree(tmp_path, index, monkeypatch):
    """A knowledge dir and a docs dir, the store, a fixed embedding, the
    ingest state kept in memory."""
    k = tmp_path / "knowledge"
    d = tmp_path / "docs"
    k.mkdir()
    d.mkdir()
    monkeypatch.setattr(ingest_mod, "KNOWLEDGE_DIR", str(k))
    monkeypatch.setattr(ingest_mod, "_DOCS_DIR", d)
    monkeypatch.setattr(ingest_mod, "_DOCS_ROOT_FILES", [])
    state: dict = {}
    monkeypatch.setattr(ingest_mod, "_load_ingest_state", lambda: dict(state))
    monkeypatch.setattr(ingest_mod, "_save_ingest_state",
                        lambda s: (state.clear(), state.update(s)))
    index.create_collection(database.GLOBAL_COLLECTION)
    monkeypatch.setattr(database, "_embed", lambda text, **kw: EMBEDDING)
    return {"knowledge": k, "docs": d, "state": state, "index": index}


def _docs_of(tree, source: str) -> list[str]:
    gen = tree["index"].get_collection(database.GLOBAL_COLLECTION)
    return gen.get(where={"source": source}, include=["documents"])["documents"]


def _errors(monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(ingest_mod, "log_error", lambda event, **kw: seen.append((event, kw)))
    return seen


# -- FIX ------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["pdf", "docx"])
@pytest.mark.parametrize("entry", ["knowledge sync", "docs sync", "watcher"])
def test_a_watched_pdf_or_docx_is_indexed_as_its_extracted_text(entry, kind, tree):
    data = _pdf(WORDS) if kind == "pdf" else _docx(WORDS)
    name = f"refunds.{kind}"
    where = tree["docs"] if entry == "docs sync" else tree["knowledge"]
    (where / name).write_bytes(data)
    source = f"docs/{name}" if entry == "docs sync" else name
    if entry == "knowledge sync":
        res = ingest_mod._sync_knowledge_dir(force=True)
        assert res.get(name, {}).get("status") == "ok", res
    elif entry == "docs sync":
        res = ingest_mod._sync_docs(force=True)
        assert res.get(source, {}).get("status") == "ok", res
    else:
        ingest_mod._handle_watched_change(False, where / name)
    stored = _docs_of(tree, source)
    assert stored, "nothing was indexed"
    joined = "\n".join(stored)
    assert WORDS in joined, joined[:200]
    assert "%PDF" not in joined and "endobj" not in joined and "PK" not in joined[:4], joined[:200]
    assert sorted(stored) == sorted(chunk_markdown_sections(extract_text(name, data)))


def test_the_watched_set_is_the_extractors():
    """One list: a type the extractor handles is watched, and only those."""
    assert ingest_mod._WATCHED_EXTS == {f".{ext}" for ext in SUPPORTED_EXTS}


@pytest.mark.parametrize("bad", ["broken", "textless"])
def test_a_pdf_with_no_text_is_skipped_with_one_bounded_log_line(bad, tree, monkeypatch):
    """The upload refuses such a file (422); the sync skips it - nothing of
    it indexed, one log line with a bounded error, and the other files in
    the tree still sync."""
    errors = _errors(monkeypatch)
    k = tree["knowledge"]
    (k / "scan.pdf").write_bytes(b"%PDF-1.4\nnot a pdf at all\n" + b"x" * 600
                                 if bad == "broken" else _pdf(None))
    (k / "faq.md").write_text("## Hours\n\nOpen nine to five.\n", encoding="utf-8")
    res = ingest_mod._sync_knowledge_dir(force=True)
    assert res.get("faq.md", {}).get("status") == "ok", res
    assert "scan.pdf" not in res, res
    assert _docs_of(tree, "scan.pdf") == []
    mine = [kw for event, kw in errors if event == "kb_file_no_text"]
    assert len(mine) == 1, errors
    assert mine[0]["file"].endswith("scan.pdf") and len(mine[0]["error"]) <= 200, mine


def test_the_corpus_probe_reads_a_pdf_by_the_syncs_rule(tree, monkeypatch):
    """_knowledge_dir_has_documents is true exactly when the sync would ingest
    something: a PDF with no text is not a document to it, one with text is."""
    k = tree["knowledge"]
    (k / "scan.pdf").write_bytes(_pdf(None))
    assert ingest_mod._knowledge_dir_has_documents() is False
    (k / "refunds.pdf").write_bytes(_pdf(WORDS))
    assert ingest_mod._knowledge_dir_has_documents() is True


# -- CONTROL: pass before the change and after ------------------------------------

def test_control_the_corpus_probe_parses_no_pdf_while_a_text_file_answers(tree, monkeypatch):
    """It runs on every self-check pass: a tree holding a text document is
    answered by the cheap read, and no PDF is parsed for it - whichever the
    walk reaches first."""
    k = tree["knowledge"]
    (k / "a-first.pdf").write_bytes(_pdf(WORDS))
    (k / "z-last.md").write_text("Open nine to five.", encoding="utf-8")
    parsed: list[str] = []
    monkeypatch.setattr(ingest_mod, "extract_text",
                        lambda name, data: parsed.append(name) or WORDS, raising=False)
    assert ingest_mod._knowledge_dir_has_documents() is True
    assert parsed == [], parsed


def test_control_an_existing_md_keeps_its_fingerprint_and_chunk_ids(tree):
    """The text types are read exactly as before. This file has CRLF line
    ends and a stray non-UTF-8 byte: read_text translates the newlines and
    the extractor's decode does not, so routing a .md through extract_text
    would move every id and the fingerprint."""
    raw = (b"## Returns\r\n\r\nReturns are accepted within thirty days.\r\n\r\n"
           b"## Refunds\r\n\r\nRefunds go to the original card. \xff\r\n")
    p = tree["knowledge"] / "policy.md"
    p.write_bytes(raw)
    before = p.read_text(encoding="utf-8", errors="ignore")      # the old read
    assert extract_text("policy.md", raw) != before, "the control would have no teeth"
    want_ids = {_cid(c, "policy.md") for c in chunk_markdown_sections(before)}
    res = ingest_mod._sync_knowledge_dir(force=True)
    assert res.get("policy.md", {}).get("status") == "ok", res
    assert set(database.get_source_ids("policy.md", "general")) == want_ids
    assert tree["state"]["policy.md"] == ingest_mod._ingest_fingerprint("policy.md", before)
