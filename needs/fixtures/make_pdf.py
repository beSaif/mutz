"""Render a plain text fixture into a small text layer PDF, with no extra dependencies.

    python -m needs.fixtures.make_pdf needs/fixtures/demo_onepager.txt needs/fixtures/demo_onepager.pdf

Helvetica, WinAnsi encoding, simple word wrap, A4. Good enough for pypdf to read back.
"""
import sys
import textwrap
from pathlib import Path

W, H = 595, 842          # A4 in points
MARGIN, SIZE, LEAD = 56, 10.5, 14.5
WRAP = 95


def _esc(s):
    out = []
    for b in s.encode("cp1252", errors="replace"):
        c = chr(b)
        if c in "()\\":
            out.append("\\" + c)
        elif 32 <= b < 127:
            out.append(c)
        else:
            out.append(f"\\{b:03o}")
    return "".join(out)


def _lines(text):
    for para in text.splitlines():
        if not para.strip():
            yield ""
            continue
        indent = "  " if para.lstrip().startswith("- ") else ""
        yield from textwrap.wrap(para, WRAP, subsequent_indent=indent) or [""]


def render(text):
    per_page = int((H - 2 * MARGIN) // LEAD)
    lines = list(_lines(text))
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objs = []                                  # index 0 = object 1

    def add(body):
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    pages_id = add(b"")                        # filled in below
    kids = []
    for page in pages:
        ops = [f"BT /F1 {SIZE} Tf {LEAD} TL {MARGIN} {H - MARGIN} Td"]
        for ln in page:
            ops.append(f"({_esc(ln)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {W} {H}] "
                        f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()))
    objs[pages_id - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_bytes(render(src.read_text()))
    print(f"wrote {dst}")
