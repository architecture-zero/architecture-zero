"""Shared text extraction for ingest surfaces.

One extractor so a file type behaves identically whichever ingest surface it
arrives by (upload endpoint today, any batch path tomorrow). Raises
ExtractError; callers decide the shape - upload maps unsupported->400 and
empty->422, a batch sync marks the file skipped and moves on.
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
