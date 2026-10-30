"""Shared text extraction for ingest surfaces.

One extractor so a file type behaves identically whichever ingest surface it
arrives by (upload endpoint today, any batch path tomorrow). Raises
ExtractError; callers decide the shape - upload maps unsupported->400 and
empty->422, a batch sync marks the file skipped and moves on. A parser
library this build lacks raises its own error instead, which parser_missing
tells apart: the server's fault, never the file's, so never "no text".
"""
import io


class ExtractError(ValueError):
    """unsupported=True means the file TYPE is not handled (vs. a handled
    type that yielded no text)."""

    def __init__(self, message: str, unsupported: bool = False):
        super().__init__(message)
        self.unsupported = unsupported


# Deliberately identical to the upload endpoint's historical list - factoring
# must not silently widen what the API accepts.
TEXT_EXTS = ("txt", "md", "py", "js", "ts", "json", "yaml", "yml")

# Every type extract_text handles, declared once (2026-10-29): the admin
# upload picker's UPLOAD_TYPES (frontend/src/admin/KBManage.tsx) is pinned to
# this by tests/test_upload_types_pin.py, so a type added here and not to the
# picker, or the reverse, fails the suite instead of drifting by hand.
SUPPORTED_EXTS = ("pdf", "docx") + TEXT_EXTS

# What a caller says when the server could not parse a type (2026-10-29): the
# upload's 500, the syncs' skipped entry and the agent's read_file error. It
# names the type only; the library's own words go to the log, bounded.
PARSER_MISSING_DETAIL = ("This server cannot read .{ext} files: a library it needs to "
                         "extract their text is missing. This is a fault on the server, "
                         "not in the file.")


def parser_missing(e: BaseException) -> bool:
    """Whether an extraction failure is the server's: a parser library this
    build does not have (2026-10-29). An ImportError - extract_text imports
    pypdf and python-docx inside itself, so a build without one raises it
    there (ModuleNotFoundError is one) - or pypdf's own DependencyError,
    raised for a library it needs to decode a stream it found (brotli, an AES
    provider). Any other exception is the file's: bytes the library could not
    read. A caller never treats this one as "the file has no text": the file
    did not fail, the build did, and a build with the library reads it."""
    if isinstance(e, ImportError):
        return True
    try:
        from pypdf.errors import DependencyError
    except ImportError:
        return False
    return isinstance(e, DependencyError)


def extract_text(name: str, data: bytes) -> str:
    """Text for ingestion from a filename + raw bytes. PDF via pypdf, docx
    via python-docx, the TEXT_EXTS decode as UTF-8 (errors ignored)."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext == "pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    elif ext == "docx":
        import docx
        doc = docx.Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
    elif ext in TEXT_EXTS:
        text = data.decode("utf-8", errors="ignore")
    else:
        raise ExtractError(f"Unsupported file type: .{ext}", unsupported=True)
    if not text.strip():
        raise ExtractError("No text could be extracted from file")
    return text
