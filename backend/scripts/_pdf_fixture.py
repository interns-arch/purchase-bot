"""A dependency-free writer for small text PDFs, used only by the checks to
build stock lists and bills shaped like real dealer printouts. Not used by
the application."""

from __future__ import annotations

from pathlib import Path


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: Path, pages: list[dict]) -> Path:
    """Each page: {"text": [(x, y, size, "string"), ...],
    "lines": [(x1, y1, x2, y2), ...]}. A4 portrait, points, origin bottom-left."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    pages_id_placeholder = len(objects) + 1 + 2 * len(pages)
    for page in pages:
        ops: list[str] = []
        for x1, y1, x2, y2 in page.get("lines", []):
            ops.append(f"{x1} {y1} m {x2} {y2} l S")
        for x, y, size, text in page.get("text", []):
            ops.append(f"BT /F1 {size} Tf {x} {y} Td ({_escape(text)}) Tj ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_id_placeholder} 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
    assert pages_id == pages_id_placeholder
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def table_page(rows: list[list[str]], columns: list[int], *, top: int = 780, title: list[str] = (), bordered: bool = False, size: int = 9) -> dict:
    """A page holding `rows` laid out at the given column x-positions."""
    text, lines = [], []
    y = top
    for line in title:
        text.append((columns[0], y, size + 2, line))
        y -= 18
    height = 14
    for row in rows:
        for x, cell in zip(columns, row):
            text.append((x + 2, y, size, cell))
        if bordered:
            lines.append((columns[0], y + height - 3, 560, y + height - 3))
        y -= height
    if bordered and rows:
        lines.append((columns[0], y + height - 3, 560, y + height - 3))
        bottom, top_edge = y + height - 3, y + height * (len(rows) + 1) - 3
        for x in list(columns) + [560]:
            lines.append((x, bottom, x, top_edge))
    return {"text": text, "lines": lines}
