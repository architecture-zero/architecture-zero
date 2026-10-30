"""A watched file the server cannot parse is skipped, never removed (2026-10-29).

app/ingest_sync.py's _read_watched returned "" for ANY exception out of the
extractor, ImportError included, and both syncs remove the file-sync rows of
a watched file that reads "" (_drop_textless_source). So a build without
pypdf - or without python-docx, or without a library pypdf needs for a
stream it found (its DependencyError) - removed every watched PDF's rows and
fingerprint at its next sync, boot included. A missing parser is the
server's fault, not the file's.

What these pin: with the parser missing, the knowledge sync and the docs sync
skip such a file - its rows and its fingerprint exactly as they were, one
log line per sync of bounded detail naming the type and the library, a
result entry that says skipped in a fixed sentence - and a sync on a build
with the parser finds it unchanged; a textless docs/ file sharing its name
does not take its rows; the watcher leaves it with that one line; the corpus
probe counts it as a document ("not required" is never assumed on an error);
the upload answers 500 with a fixed sentence and logs the library's words,
bounded. The CONTROL cases pass before the change too: a corrupt PDF still
leaves the index as the textless rule says and still answers the upload's
422; a text file syncs as before while a parser is missing.

The store is test_knowledge_orphan_prune's index fixture, real in-memory
chroma and the fake, through test_knowledge_sync_reads_through_extractor's
tree fixture.
"""
import sys

import pytest
from fastapi import HTTPException

import app.database as database
import app.ingest_sync as ingest_mod
from app import text_extract
from app.routers import kb
from tests.test_knowledge_orphan_prune import EMBEDDING, index  # noqa: F401
from tests.test_knowledge_sync_reads_through_extractor import (  # noqa: F401
    WORDS, _docx, _pdf, tree)

TEXT = "## Hours\n\nOpen nine to five.\n"


def _without(module):
    """The build without this library: `import <module>` raises ImportError
    (a None in sys.modules halts the import)."""
    def setup(mp):
        mp.setitem(sys.modules, module, None)
    return setup


def _pypdf_dependency_missing(mp):
    """pypdf present, a library it needs for this file's streams not: the
    DependencyError it raises for that."""
    import pypdf
    from pypdf.errors import DependencyError

    def reader(*a, **k):
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")
    mp.setattr(pypdf, "PdfReader", reader)


# What the libraries' own messages say, which never reaches a result or a
# response (the fixed sentence names the type, ".docx", and nothing else).
LIBRARY_WORDS = ("import of", "halted", "sys.modules", "pypdf", "cryptography", "AES")

# (file name, its bytes, the missing parser, a word naming the library)
CASES = [
    pytest.param("refunds.pdf", lambda: _pdf(WORDS), _without("pypdf"), "pypdf", id="no-pypdf"),
    pytest.param("refunds.pdf", lambda: _pdf(WORDS), _pypdf_dependency_missing, "cryptography",
                 id="pypdf-dependency"),
    pytest.param("refunds.docx", lambda: _docx(WORDS), _without("docx"), "docx",
                 id="no-python-docx"),
]


def _rows(tree, source: str) -> list:
    got = tree["index"].get_collection(database.GLOBAL_COLLECTION).get(
        where={"source": source}, include=["documents", "metadatas"])
    return sorted(zip(got["ids"], got["documents"],
                      [sorted(m.items()) for m in got["metadatas"]]))


def _logged(monkeypatch) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(ingest_mod, "log_error", lambda event, **kw: seen.append((event, kw)))
    monkeypatch.setattr(ingest_mod, "log", lambda event, **kw: seen.append((event, kw)))
    return seen


def _where(tree, entry, name):
    if entry == "docs sync":
        return tree["docs"], f"docs/{name}", ingest_mod._sync_docs
    return tree["knowledge"], name, ingest_mod._sync_knowledge_dir


def _reason(name: str) -> str:
    return text_extract.PARSER_MISSING_DETAIL.format(ext=name.rsplit(".", 1)[-1])


# -- FIX ------------------------------------------------------------------------

@pytest.mark.parametrize("name, make, missing, library", CASES)
@pytest.mark.parametrize("entry", ["knowledge sync", "docs sync"])
def test_fix_a_missing_parser_leaves_an_indexed_file_as_it_was(
        entry, name, make, missing, library, tree, monkeypatch):
    where, source, sync = _where(tree, entry, name)
    (where / name).write_bytes(make())
    assert sync(force=True)[source]["status"] == "ok"
    rows = _rows(tree, source)
    assert rows and any(WORDS in doc for _, doc, _ in rows)
    fingerprint = tree["state"][source]

    seen = _logged(monkeypatch)
    with monkeypatch.context() as mp:
        missing(mp)
        for n, force in enumerate((False, True), start=1):     # the boot sync, then a forced one
            res = sync(force=force)
            assert _rows(tree, source) == rows, (
                f"a missing parser removed the file's rows (force={force}): {res.get(source)}")
            assert tree["state"].get(source) == fingerprint, "the fingerprint went"
            assert res.get(source) == {"status": "skipped", "reason": _reason(name)}, res
            assert not any(w in res[source]["reason"] for w in LIBRARY_WORDS), res[source]
            mine = [kw for event, kw in seen if event == "kb_file_parser_missing"]
            assert len(mine) == n, seen
            assert mine[-1]["file"].endswith(name) and mine[-1]["ext"] == name.rsplit(".", 1)[-1]
            assert library in mine[-1]["error"] and len(mine[-1]["error"]) <= 300, mine[-1]
            assert not [e for e, _ in seen if e in ("kb_file_no_text", "kb_file_no_text_removed")], seen

    # The parser back: the next sync reads the file, finds it unchanged.
    assert sync(force=False)[source] == {"status": "skipped"}
    assert _rows(tree, source) == rows


def test_fix_a_textless_docs_file_of_its_name_takes_none_of_its_rows(tree, monkeypatch):
    """docs/ sources are keyed by file name. A file of the name the server
    cannot parse holds the name as a file with text does: a textless one
    elsewhere in docs/ is not a reason to remove its rows. The shape that can
    happen: pypdf present, and only the indexed PDF needs the library pypdf
    lacks (an encrypted one, say) - the textless one parses, to no text."""
    import pypdf
    from pypdf.errors import DependencyError
    d = tree["docs"]
    (d / "a").mkdir()
    (d / "b").mkdir()
    (d / "a" / "guide.pdf").write_bytes(_pdf(WORDS))
    assert ingest_mod._sync_docs(force=True)["docs/guide.pdf"]["status"] == "ok"
    rows = _rows(tree, "docs/guide.pdf")
    assert rows
    (d / "b" / "guide.pdf").write_bytes(_pdf(None))
    real = pypdf.PdfReader

    def reader(stream, *a, **k):
        if WORDS.encode() in stream.getvalue():
            raise DependencyError("cryptography>=3.1 is required for AES algorithm")
        return real(stream, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(pypdf, "PdfReader", reader)
        res = ingest_mod._sync_docs(force=True)
    assert _rows(tree, "docs/guide.pdf") == rows, res


@pytest.mark.parametrize("name, make, missing, library", CASES)
@pytest.mark.parametrize("deleted", [False, True], ids=["changed", "replaced"])
def test_fix_the_watcher_leaves_it_with_one_line_naming_the_library(
        deleted, name, make, missing, library, tree, monkeypatch):
    p = tree["knowledge"] / name
    p.write_bytes(make())
    assert ingest_mod._sync_knowledge_dir(force=True)[name]["status"] == "ok"
    rows = _rows(tree, name)
    seen = _logged(monkeypatch)
    with monkeypatch.context() as mp:
        missing(mp)
        ingest_mod._handle_watched_change(deleted, p)
    assert _rows(tree, name) == rows
    assert [e for e, _ in seen] == ["kb_file_parser_missing"], seen
    assert library in seen[0][1]["error"] and len(seen[0][1]["error"]) <= 300


def test_fix_the_corpus_probe_counts_a_file_it_cannot_parse_as_a_document(tree, monkeypatch):
    """A tree of PDFs on a build without pypdf holds the operator's documents:
    an empty store beside them is an error, not "not required"."""
    (tree["knowledge"] / "refunds.pdf").write_bytes(_pdf(WORDS))
    seen = _logged(monkeypatch)
    with monkeypatch.context() as mp:
        _without("pypdf")(mp)
        assert ingest_mod._knowledge_dir_has_documents() is True
    assert seen == [], "the probe runs every self-check pass and logs nothing"


@pytest.mark.parametrize("name, make, missing, library", CASES)
def test_fix_the_upload_answers_a_missing_parser_with_a_5xx_and_a_fixed_sentence(
        name, make, missing, library, monkeypatch):
    ext = name.rsplit(".", 1)[-1]
    data = make()
    logged: list[tuple[str, dict]] = []
    monkeypatch.setattr(kb, "log_error", lambda event, **kw: logged.append((event, kw)))
    missing(monkeypatch)
    with pytest.raises(HTTPException) as raised:
        kb._ingest_upload(name, ext, data, "general", {"id": 1, "role": "admin"})
    status, detail = raised.value.status_code, raised.value.detail
    assert 500 <= status < 600, f"a missing parser answered {status} {detail!r}"
    assert detail == _reason(name)
    assert not any(w in detail for w in LIBRARY_WORDS), detail
    assert [e for e, _ in logged] == ["ingest_parser_missing"], logged
    assert library in logged[0][1]["error"] and len(logged[0][1]["error"]) <= 300


# -- CONTROL: pass before the change and after ------------------------------------

def test_control_a_corrupt_pdf_still_leaves_the_index(tree):
    k = tree["knowledge"]
    (k / "refunds.pdf").write_bytes(_pdf(WORDS))
    assert ingest_mod._sync_knowledge_dir(force=False)["refunds.pdf"]["status"] == "ok"
    (k / "refunds.pdf").write_bytes(b"%PDF-1.4\nnot a pdf at all\n")
    res = ingest_mod._sync_knowledge_dir(force=False)
    assert res.get("refunds.pdf", {}).get("status") == "pruned", res
    assert _rows(tree, "refunds.pdf") == []
    assert "refunds.pdf" not in tree["state"]


def test_control_a_text_file_syncs_as_before_while_a_parser_is_missing(tree, monkeypatch):
    k = tree["knowledge"]
    (k / "faq.md").write_text(TEXT, encoding="utf-8")
    with monkeypatch.context() as mp:
        _without("pypdf")(mp)
        _without("docx")(mp)
        assert ingest_mod._sync_knowledge_dir(force=False)["faq.md"]["status"] == "ok"
        assert _rows(tree, "faq.md")
        (k / "faq.md").write_text("", encoding="utf-8")
        res = ingest_mod._sync_knowledge_dir(force=False)
    assert res.get("faq.md", {}).get("status") == "pruned", res
    assert _rows(tree, "faq.md") == []


def test_control_the_upload_answers_a_corrupt_pdf_with_its_422():
    with pytest.raises(HTTPException) as raised:
        kb._ingest_upload("a.pdf", "pdf", b"not a pdf at all", "general",
                          {"id": 1, "role": "admin"})
    assert raised.value.status_code == 422
    assert raised.value.detail.startswith("Failed to extract text: "), raised.value.detail
