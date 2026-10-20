"""Text out of PDF, DOCX and XLSX attachments, on this machine (TD-5131).

A document attachment is not sent to the model as a file. The daemon turns
it into plain text here, with a marker line for every PDF page and every
XLSX sheet, and the text is what rides in the message, under the same caps
as an attached text file. The caps measure the *text*, not the file: a
6 MB PDF of scanned pages with 20 KB of text is fine, and a small XLSX whose
cells expand to more text than the cap is not (``attachments.py``).

No network, ever (directive 3). PDF goes through PDFium (``pypdfium2``),
which reads only the bytes handed to it. DOCX and XLSX are zip files of XML
and are read with the standard library alone.

Hostile files are expected. A zip member is read only up to
:data:`MAX_XML_BYTES` after decompression, so a zip bomb stops at the cap
instead of filling memory. XML with a DOCTYPE is refused outright: Office
never writes one, and it is the door to entity-expansion attacks. Extraction
stops as soon as the text passes the caller's cap rather than finishing a
million-page file only to refuse it.

Deliberately leaf, like ``attachments.py``: nothing from ``tstd`` is
imported. Blocking: call from a worker thread.
"""

from __future__ import annotations

import io
import re
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree

DocumentKind = Literal["pdf", "docx", "xlsx"]

# Decompressed bytes one zip member may cost. A real document.xml or
# sheet1.xml past this is beyond anything an attachment cap would take.
MAX_XML_BYTES = 64 * 1024 * 1024

# Zip members a DOCX or XLSX may list. Real ones have dozens.
MAX_ZIP_MEMBERS = 10_000

# PDFium is not thread-safe, and attachments are extracted in worker
# threads, so one document at a time goes through it.
PDFIUM_LOCK = threading.Lock()


class DocumentError(Exception):
    """A document that could not be turned into text, with its wire code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ExtractedDocument:
    """The text of one document, and how many pages or sheets it held."""

    kind: DocumentKind
    text: str
    # Pages for a PDF, sheets for an XLSX, 0 for a DOCX (Word has no pages
    # until it lays the text out, so there is nothing honest to count).
    units: int


class _TooLong(Exception):
    """The text passed the cap; extraction stops early."""


def sniff_document(raw: bytes) -> DocumentKind | None:
    """PDF, DOCX or XLSX from the bytes themselves, never the file name.

    A zip is a DOCX when it holds ``word/document.xml`` and an XLSX when it
    holds ``xl/workbook.xml``. Any other zip, and anything else, is None.
    """
    # The header must lead. Readers forgive junk before it, but a text file
    # that merely mentions "%PDF-" near the top must stay a text file.
    if raw.startswith(b"%PDF-"):
        return "pdf"
    if not raw.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist()[:MAX_ZIP_MEMBERS])
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return None


def extract_document(
    name: str, kind: DocumentKind, raw: bytes, *, max_text_bytes: int
) -> ExtractedDocument:
    """Plain text of *raw*, or :class:`DocumentError`.

    ``attachment_too_large`` when the text passes *max_text_bytes* (UTF-8);
    ``attachment_unreadable`` when the file is damaged, locked or not what
    its bytes claimed; ``attachment_no_text`` when it holds no text at all
    (a scanned PDF, for one).
    """
    sink = _Sink(max_text_bytes)
    try:
        if kind == "pdf":
            units = _pdf(name, raw, sink)
        elif kind == "docx":
            _docx(name, raw, sink)
            units = 0
        else:
            units = _xlsx(name, raw, sink)
    except _TooLong:
        raise DocumentError(
            "attachment_too_large",
            f"The text in {name} is longer than this workspace allows per file "
            f"({_fmt(max_text_bytes)} of text). Attach a shorter document, or "
            "raise max_file_bytes under attachments: in .tst/config.yaml. "
            "Nothing was sent.",
        ) from None
    if not sink.has_content:
        raise DocumentError(
            "attachment_no_text",
            f"{name} has no text to read (a scanned PDF holds only pictures of "
            "pages). Attach a version with text in it. Nothing was sent.",
        )
    return ExtractedDocument(kind=kind, text=sink.text(), units=units)


class _Sink:
    """Collects lines and stops the moment their UTF-8 size passes the cap."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._size = 0
        self._parts: list[str] = []
        self.has_content = False

    def add(self, line: str, *, marker: bool = False) -> None:
        if self._parts:
            self._size += 1  # the newline joining it to the last line
        self._size += len(line.encode("utf-8"))
        if self._size > self._cap:
            raise _TooLong
        self._parts.append(line)
        if not marker and line.strip():
            self.has_content = True

    def text(self) -> str:
        return "\n".join(self._parts)


def _fmt(count: int) -> str:
    if count < 1000:
        return f"{count} bytes"
    if count < 1_000_000:
        return f"{count / 1000:.1f} KB"
    return f"{count / 1_000_000:.1f} MB"


def _unreadable(name: str, what: str) -> DocumentError:
    return DocumentError(
        "attachment_unreadable",
        f"{name} couldn't be read as a {what}: it may be damaged or locked "
        "with a password. Re-save it, or attach it as text. Nothing was sent.",
    )


# --- PDF ---------------------------------------------------------------


def _pdf(name: str, raw: bytes, sink: _Sink) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # a build without the wheel
        raise DocumentError(
            "attachment_unreadable",
            f"{name} is a PDF, but PDF support is not installed in this build. Nothing was sent.",
        ) from e

    with PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(raw)
        except pdfium.PdfiumError as e:
            raise _unreadable(name, "PDF") from e
        try:
            count = len(pdf)
            for index in range(count):
                sink.add(f"[page {index + 1} of {count}]", marker=True)
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_bounded()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                cleaned = _clean(text)
                if cleaned:
                    for line in cleaned.split("\n"):
                        sink.add(line)
            return count
        finally:
            pdf.close()


def _clean(text: str) -> str:
    """PDFium's CRLFs and form feeds as plain newlines, trailing space gone."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = text.replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


# --- Office (zip of XML) -----------------------------------------------


def _open_zip(name: str, raw: bytes, what: str) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, ValueError, OSError) as e:
        raise _unreadable(name, what) from e
    if len(zf.infolist()) > MAX_ZIP_MEMBERS:
        zf.close()
        raise _unreadable(name, what)
    return zf


def _read_member(zf: zipfile.ZipFile, member: str, name: str, what: str) -> bytes | None:
    """One member's bytes, capped after decompression; None when absent."""
    try:
        info = zf.getinfo(member)
    except KeyError:
        return None
    try:
        with zf.open(info) as fh:
            data = fh.read(MAX_XML_BYTES + 1)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError) as e:
        # RuntimeError: encrypted member; NotImplementedError: odd compression.
        raise _unreadable(name, what) from e
    if len(data) > MAX_XML_BYTES:
        raise DocumentError(
            "attachment_too_large",
            f"{name} expands to more than {_fmt(MAX_XML_BYTES)} inside, which is "
            "more than an attachment can carry. Nothing was sent.",
        )
    return data


def _parse(data: bytes, name: str, what: str) -> ElementTree.Element:
    # Office never writes a DTD; one here is an entity-expansion attempt.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise _unreadable(name, what)
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise _unreadable(name, what) from e
    # Match on local names: Transitional and Strict OOXML use different
    # namespace URIs for the same parts, and neither changes the text.
    for el in root.iter():
        if isinstance(el.tag, str):
            el.tag = _local(el.tag)
        if el.attrib:
            el.attrib = {_local(k): v for k, v in el.attrib.items()}
    return root


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _docx(name: str, raw: bytes, sink: _Sink) -> None:
    with _open_zip(name, raw, "Word document") as zf:
        data = _read_member(zf, "word/document.xml", name, "Word document")
        if data is None:
            raise _unreadable(name, "Word document")
        root = _parse(data, name, "Word document")
    body = root.find("body")
    if body is None:
        return
    for block in body:
        if block.tag == "p":
            sink.add(_paragraph(block))
        elif block.tag == "tbl":
            for row in block.iter("tr"):
                cells = [
                    " ".join(_paragraph(p) for p in cell.iter("p")).strip()
                    for cell in row.findall("tc")
                ]
                sink.add("\t".join(cells))
        else:
            # Content controls (w:sdt) and the like wrap ordinary paragraphs.
            for p in block.iter("p"):
                sink.add(_paragraph(p))


def _paragraph(p: ElementTree.Element) -> str:
    out: list[str] = []
    for node in p.iter():
        if node.tag == "t" and node.text:
            out.append(node.text)
        elif node.tag == "tab":
            out.append("\t")
        elif node.tag in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _xlsx(name: str, raw: bytes, sink: _Sink) -> int:
    what = "spreadsheet"
    with _open_zip(name, raw, what) as zf:
        shared = _shared_strings(zf, name)
        sheets = list(_sheet_parts(zf, name))
        for sheet_name, part in sheets:
            sink.add(f"[sheet: {sheet_name}]", marker=True)
            data = _read_member(zf, part, name, what)
            if data is None:
                continue
            root = _parse(data, name, what)
            for row in root.iter("row"):
                values = _row_values(row, shared)
                if values:
                    sink.add("\t".join(values).rstrip("\t"))
    return len(sheets)


def _shared_strings(zf: zipfile.ZipFile, name: str) -> list[str]:
    data = _read_member(zf, "xl/sharedStrings.xml", name, "spreadsheet")
    if data is None:
        return []
    root = _parse(data, name, "spreadsheet")
    return ["".join(t.text or "" for t in si.iter("t")) for si in root.findall("si")]


def _sheet_parts(zf: zipfile.ZipFile, name: str) -> Iterator[tuple[str, str]]:
    """(sheet name, zip member) in workbook order."""
    what = "spreadsheet"
    book = _read_member(zf, "xl/workbook.xml", name, what)
    if book is None:
        raise _unreadable(name, what)
    targets: dict[str, str] = {}
    rels = _read_member(zf, "xl/_rels/workbook.xml.rels", name, what)
    if rels is not None:
        for rel in _parse(rels, name, what).iter("Relationship"):
            target = rel.get("Target", "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            targets[rel.get("Id", "")] = target
    sheets = _parse(book, name, what).find("sheets")
    if sheets is None:
        return
    for i, sheet in enumerate(sheets.findall("sheet"), start=1):
        part = targets.get(sheet.get("id", ""), f"xl/worksheets/sheet{i}.xml")
        yield sheet.get("name", f"Sheet{i}"), part


_COLUMN = re.compile(r"[A-Z]+")


def _column_index(ref: str | None) -> int | None:
    if not ref:
        return None
    m = _COLUMN.match(ref)
    if m is None:
        return None
    index = 0
    for ch in m.group(0):
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _row_values(row: ElementTree.Element, shared: list[str]) -> list[str]:
    """Cell values in column order, blanks kept so columns still line up."""
    values: list[str] = []
    for cell in row.findall("c"):
        col = _column_index(cell.get("r"))
        # A sparse row skips empty cells; pad so column C stays third. A
        # far-right reference (XFD1) pads at most to Excel's 16,384 columns,
        # and the text cap still holds the total.
        if col is not None and col > len(values):
            values.extend([""] * min(col - len(values), 16_384))
        # A line break inside a cell would read as a new row.
        values.append(" ".join(_cell_text(cell, shared).split("\n")))
    while values and values[-1] == "":
        values.pop()
    return values


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find("is")
        if inline is None:
            return ""
        return "".join(t.text or "" for t in inline.iter("t"))
    v = cell.find("v")
    raw = v.text if v is not None and v.text is not None else ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE" if raw == "0" else raw
    return raw
