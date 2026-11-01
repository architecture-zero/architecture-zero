"""PDF and Word files are parsed in one place only: the extractor service (2026-11-01).

The backend parsed them in its own process with pypdf and python-docx, so a
file built to hang the parser or exhaust its memory - every pypdf bug so far
has been one of those, and a PDF from outside reaches the parser through an
upload, a sync or a connector - cost the backend itself, with all its
secrets and data beside it. Parsing moved to a locked container that has
none of those (extractor/), and the backend lost both libraries.

What these pin: no backend module imports a document parser; the backend's
requirements carry none; every PDF and docx read goes to the service; the
text types still decode here and never reach it. Each fails on the code
before the move (raising=False in conftest's stub fixture, so it fails here
and not in setup).
"""
import ast
import pathlib

import pytest

from app import text_extract
from tests.extractor_stub import STUB, register

BACKEND = pathlib.Path(__file__).resolve().parents[1]
PARSERS = ("pypdf", "PyPDF2", "docx", "fitz", "pdfminer", "pdfplumber", "pypdfium2")


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_no_backend_module_imports_a_document_parser():
    offenders = {str(p.relative_to(BACKEND)): sorted(_imports(p) & set(PARSERS))
                 for p in sorted((BACKEND / "app").rglob("*.py"))}
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"parsing belongs to the extractor service: {offenders}"


def test_the_backend_requirements_carry_no_document_parser():
    names = []
    for line in (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line.split("==")[0].split("[")[0].strip().lower())
    assert not set(names) & {"pypdf", "pypdf2", "python-docx", "pymupdf", "pdfminer.six",
                             "pdfplumber", "pypdfium2"}, names


@pytest.mark.parametrize("name", ["policy.pdf", "policy.docx", "POLICY.PDF"])
def test_every_document_goes_to_the_service(name):
    data = register(b"%stand-in bytes for " + name.encode(), "Refunds within thirty days")
    assert text_extract.extract_text(name, data) == "Refunds within thirty days"
    assert STUB.calls == [(name.rsplit(".", 1)[-1].lower(), len(data))], STUB.calls


@pytest.mark.parametrize("name", ["notes.md", "faq.txt", "config.yaml"])
def test_text_types_decode_here_and_never_reach_the_service(name):
    assert text_extract.extract_text(name, "Open nine to five".encode()) == "Open nine to five"
    assert STUB.calls == [], STUB.calls
