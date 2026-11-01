"""Real files for the extractor's tests, built in memory."""
import io
import zlib


def pdf(pages: list[str | None]) -> bytes:
    """A PDF built by hand: one page per entry, each one line of Helvetica
    text (None gives a page with no text), a correct xref table."""
    n_pages = len(pages)
    # Objects: 1 catalog, 2 pages, 3 font, then a (page, contents) pair each.
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        content = (f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET" if text is not None else "").encode()
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Contents {5 + 2 * i} 0 R /Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                       + content + b"\nendstream")
    return _assemble(objects)


def bomb_pdf(inflated_bytes: int) -> bytes:
    """A one-page PDF whose content stream is small on disk and inflates to
    `inflated_bytes` - a decompression bomb, built without ever holding the
    inflated bytes in memory."""
    comp = zlib.compressobj(9)
    chunk = b" " * (1024 * 1024)
    body = bytearray()
    for _ in range(inflated_bytes // len(chunk)):
        body += comp.compress(chunk)
    body += comp.flush()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length " + str(len(body)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + bytes(body) + b"\nendstream",
    ]
    return _assemble(objects)


def _assemble(objects: list[bytes]) -> bytes:
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


def docx(paragraphs: list[str]) -> bytes:
    import docx as python_docx
    doc = python_docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
