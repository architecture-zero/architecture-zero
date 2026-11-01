"""One file, one process: the parse itself.

server.py starts this script once per file, under a clock and a memory cap,
with the file's bytes on stdin and its type as the only argument. It prints
one JSON line and exits. Nothing here reads the network, the environment or
the disk; the box this runs in has none of those to offer anyway.

The answer is {"ok": true, "text": ..., "truncated": bool} or
{"ok": false, "kind": ..., "detail": ...}. kind is one of:
  empty               the file parsed and holds no text
  unreadable          the file could not be parsed (its own fault)
  memory              the parse ran out of its memory cap
  parser_missing      this build lacks pypdf or python-docx (the build's fault)
  dependency_missing  pypdf lacks a library a stream in this file needs
                      (brotli, an AES provider) - left out of the box on
                      purpose, so such a file is never decoded here
detail is the exception's type and words, bounded; it goes to a log line,
never to a user.
"""
import io
import json
import sys

# Bounds on what one file can produce. Far above any real document (5M
# characters is about 2,500 dense pages); a file that reaches either is cut
# there, and the answer says so.
MAX_PAGES = 5000
MAX_TEXT_CHARS = 5_000_000


def _extract(ext: str, data: bytes) -> str:
    if ext == "pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        parts = []
        for n, page in enumerate(reader.pages):
            if n >= MAX_PAGES:
                break
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    if ext == "docx":
        import docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    raise ValueError(f"unsupported type: {ext[:16]}")


def _detail(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def answer(ext: str, data: bytes) -> dict:
    try:
        text = _extract(ext, data)
    except MemoryError as e:
        return {"ok": False, "kind": "memory", "detail": _detail(e)}
    except ImportError as e:
        return {"ok": False, "kind": "parser_missing", "detail": _detail(e)}
    except Exception as e:
        try:
            from pypdf.errors import DependencyError
        except ImportError:
            DependencyError = ()
        if DependencyError and isinstance(e, DependencyError):
            return {"ok": False, "kind": "dependency_missing", "detail": _detail(e)}
        return {"ok": False, "kind": "unreadable", "detail": _detail(e)}
    if not text.strip():
        return {"ok": False, "kind": "empty", "detail": ""}
    truncated = len(text) > MAX_TEXT_CHARS
    return {"ok": True, "text": text[:MAX_TEXT_CHARS], "truncated": truncated}


def main() -> None:
    ext = sys.argv[1] if len(sys.argv) > 1 else ""
    data = sys.stdin.buffer.read()
    sys.stdout.write(json.dumps(answer(ext, data)) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
