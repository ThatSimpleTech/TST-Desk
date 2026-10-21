"""Small, real PDF, DOCX and XLSX files built in memory for tests (TD-5131).

Built by hand rather than checked in, so each test says exactly what is in
the file it reads.
"""

from __future__ import annotations

import io
import zipfile


def make_pdf(pages: list[str], *, padding: int = 0) -> bytes:
    """A PDF with one line of Helvetica text per page.

    *padding* adds an unreferenced stream of that many bytes, so the file
    can be made far bigger than its text. An empty string is a page with no
    text (a scanned page, as far as text goes).
    """
    objects: list[bytes] = []
    count = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    font_id = 3 + 2 * count
    objects.append(b"<</Type/Catalog/Pages 2 0 R>>")
    objects.append(f"<</Type/Pages/Kids[{kids}]/Count {count}>>".encode())
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            (
                f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
                f"/Resources<</Font<</F1 {font_id} 0 R>>>>/Contents {content_id} 0 R>>"
            ).encode()
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 18 Tf 72 700 Td ({escaped}) Tj ET".encode() if text else b""
        objects.append(b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    if padding:
        filler = b"0" * padding
        objects.append(b"<</Length %d>>\nstream\n" % len(filler) + filler + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<</Size %d/Root 1 0 R>>\n" % (len(objects) + 1))
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    """A Word document: the paragraphs, then an optional table."""
    body = "".join(f"<w:p><w:r><w:t>{_esc(p)}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join(
            "<w:tr>"
            + "".join(f"<w:tc><w:p><w:r><w:t>{_esc(c)}</w:t></w:r></w:p></w:tc>" for c in row)
            + "</w:tr>"
            for row in table
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    head = '<?xml version="1.0" encoding="UTF-8"?>'
    xml = f"{head}<w:document {_W}><w:body>{body}</w:body></w:document>"
    return _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": xml})


_S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_PR = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def make_xlsx(sheets: dict[str, list[list[str | int]]]) -> bytes:
    """A workbook; strings go to the shared table, numbers stay numbers.

    Sheet parts are numbered backwards on purpose, so a reader that guesses
    ``sheetN.xml`` from the order instead of following the relationships
    gets the names wrong.
    """
    shared: list[str] = []
    files: dict[str, str] = {"[Content_Types].xml": "<Types/>"}
    entries = []
    rels = []
    n = len(sheets)
    for i, (name, rows) in enumerate(sheets.items(), start=1):
        part = n - i + 1
        entries.append(f'<sheet name="{_esc(name)}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(
            f'<Relationship Id="rId{i}" Type="worksheet" Target="worksheets/sheet{part}.xml"/>'
        )
        row_xml = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                if value == "":
                    continue  # sparse: empty cells are left out, as Excel does
                ref = f"{chr(65 + c)}{r}"
                if isinstance(value, int):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    shared.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            row_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
        files[f"xl/worksheets/sheet{part}.xml"] = (
            f"<worksheet {_S}><sheetData>{''.join(row_xml)}</sheetData></worksheet>"
        )
    files["xl/workbook.xml"] = f"<workbook {_S} {_R}><sheets>{''.join(entries)}</sheets></workbook>"
    files["xl/_rels/workbook.xml.rels"] = f"<Relationships {_PR}>{''.join(rels)}</Relationships>"
    files["xl/sharedStrings.xml"] = (
        f"<sst {_S}>" + "".join(f"<si><t>{_esc(s)}</t></si>" for s in shared) + "</sst>"
    )
    return _zip(files)


def _zip(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return out.getvalue()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def raw_xlsx(sheets: list[str]) -> bytes:
    """An XLSX whose sheets hold exactly the given ``<sheetData>`` XML.

    For hostile shapes make_xlsx will not build: empty rows, far-right
    cells. Sheets are named S1, S2, ... and found by their default part.
    """
    entries = "".join(f'<sheet name="S{i}" sheetId="{i}"/>' for i in range(1, len(sheets) + 1))
    files = {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": f"<workbook {_S}><sheets>{entries}</sheets></workbook>",
    }
    for i, body in enumerate(sheets, start=1):
        files[f"xl/worksheets/sheet{i}.xml"] = (
            f"<worksheet {_S}><sheetData>{body}</sheetData></worksheet>"
        )
    return _zip(files)
