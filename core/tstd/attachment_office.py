"""Text out of DOCX and XLSX attachments: zip files of XML (TD-5131).

Read with the standard library alone. Hostile files are expected. A zip
member is read only up to :data:`MAX_XML_BYTES` after decompression, and one
document's members only up to :data:`MAX_TOTAL_XML_BYTES` and
:data:`MAX_XML_ELEMENTS` in all, so a zip bomb stops after a fixed amount
of work even when it holds no text for the text cap to catch. Every part is
streamed and each element dropped once read, so memory stays small however
big a part is. XML with a DOCTYPE is refused outright: Office never writes
one, and it is the door to entity-expansion attacks.

Leaf: nothing from ``tstd`` beyond the sibling ``attachment_text``.
Blocking: call from a worker thread.
"""

from __future__ import annotations

import codecs
import io
import lzma
import re
import zipfile
import zlib
from collections.abc import Iterator
from xml.etree import ElementTree

from .attachment_text import DocumentError, Sink, fmt, unreadable

# Decompressed bytes one zip member may cost. A real document.xml or
# sheet1.xml past this is beyond anything an attachment cap would take.
MAX_XML_BYTES = 64 * 1024 * 1024

# Zip members a DOCX or XLSX may list. Real ones have dozens.
MAX_ZIP_MEMBERS = 10_000

# What one whole document may cost, across every member read: decompressed
# XML, and elements parsed. A part with no text adds nothing to the text
# cap, so without these a sheet of empty rows would be parsed to the end
# however many there were. Four million elements is a sheet of over a
# million filled cells, several times the text the default cap takes, and
# about ten seconds of parsing at worst.
MAX_TOTAL_XML_BYTES = 128 * 1024 * 1024
MAX_XML_ELEMENTS = 4_000_000


class _Budget:
    """Decompressed bytes and XML elements one document may cost in all."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._bytes = MAX_TOTAL_XML_BYTES
        self._elements = MAX_XML_ELEMENTS

    def spend_bytes(self, count: int) -> None:
        self._bytes -= count
        if self._bytes < 0:
            raise self._exhausted()

    def spend_element(self) -> None:
        self._elements -= 1
        if self._elements < 0:
            raise self._exhausted()

    def _exhausted(self) -> DocumentError:
        return DocumentError(
            "attachment_too_large",
            f"{self._name} holds more inside than an attachment can carry "
            f"(over {fmt(MAX_TOTAL_XML_BYTES)} of XML or "
            f"{MAX_XML_ELEMENTS:,} elements). Nothing was sent.",
        )


def _open_zip(name: str, raw: bytes, what: str) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, NotImplementedError, ValueError, OSError) as e:
        raise unreadable(name, what) from e
    if len(zf.infolist()) > MAX_ZIP_MEMBERS:
        zf.close()
        raise unreadable(name, what)
    return zf


def _read_member(
    zf: zipfile.ZipFile, member: str, name: str, what: str, budget: _Budget
) -> bytes | None:
    """One member's bytes, capped after decompression; None when absent."""
    try:
        info = zf.getinfo(member)
    except KeyError:
        return None
    try:
        with zf.open(info) as fh:
            data = fh.read(MAX_XML_BYTES + 1)
    except (
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        OSError,
        EOFError,
        zlib.error,
        lzma.LZMAError,
    ) as e:
        # RuntimeError: encrypted member; NotImplementedError: odd compression;
        # zlib.error / LZMAError: a damaged stream inside the member.
        raise unreadable(name, what) from e
    if len(data) > MAX_XML_BYTES:
        raise DocumentError(
            "attachment_too_large",
            f"{name} expands to more than {fmt(MAX_XML_BYTES)} inside, which is "
            "more than an attachment can carry. Nothing was sent.",
        )
    budget.spend_bytes(len(data))
    return data


def _events(
    data: bytes, name: str, what: str, budget: _Budget
) -> Iterator[tuple[str, str, ElementTree.Element]]:
    """(event, local tag, element) for every start and end in *data*.

    An element's children are gone by the time its end arrives: each one is
    removed from its parent once the caller has handled its own end. Tags
    are local names, since Transitional and Strict OOXML use different
    namespace URIs for the same parts and neither changes the text.
    """
    # Office never writes a DTD; one here is an entity-expansion attempt.
    if _declares_dtd(data):
        raise unreadable(name, what)
    stack: list[ElementTree.Element] = []
    try:
        for event, el in ElementTree.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = _local(el.tag) if isinstance(el.tag, str) else ""
            if event == "start":
                stack.append(el)
                yield event, tag, el
                continue
            budget.spend_element()
            stack.pop()
            yield event, tag, el
            if stack:
                # Every earlier child already went, so this is the only one.
                stack[-1].remove(el)
    except ElementTree.ParseError as e:
        raise unreadable(name, what) from e


# The encoding an XML declaration names.
_DECLARED_ENCODING = re.compile(r"""<\?xml[^>]*?encoding\s*=\s*["']([^"']*)["']""")
_UTF16_NAMES = frozenset({"utf-16", "utf16", "utf-16le", "utf-16be"})


def _declares_dtd(data: bytes) -> bool:
    """Whether *data* has a DTD, or might have one this test cannot see.

    A byte search for ``<!DOCTYPE`` only sees parts whose encoding writes it
    in ASCII, and the parser also reads UTF-16 and whatever 8-bit encoding a
    declaration names. So UTF-16 is decoded as the parser would decode it,
    and a part declaring an encoding that spells the markers some other way
    (EBCDIC, UTF-32) counts as having one: OOXML parts are UTF-8 or UTF-16
    (ECMA-376 Part 2), and nothing here could read what it declares.
    """
    utf16 = _utf16_codec(data)
    if utf16 is not None:
        try:
            text = data.decode(utf16)
        except UnicodeDecodeError:
            return True
        declared = _DECLARED_ENCODING.match(text.lstrip("\ufeff"))
        if declared is not None and declared.group(1).lower() not in _UTF16_NAMES:
            return True
        return "<!DOCTYPE" in text or "<!ENTITY" in text
    head = data[:512].decode("latin-1").removeprefix("\xef\xbb\xbf")
    declared = _DECLARED_ENCODING.match(head)
    if declared is not None:
        try:
            spelled = "<!DOCTYPE<!ENTITY".encode(declared.group(1))
        except (LookupError, UnicodeError):
            return True
        if spelled != b"<!DOCTYPE<!ENTITY":
            return True
    return b"<!DOCTYPE" in data or b"<!ENTITY" in data


def _utf16_codec(data: bytes) -> str | None:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if data.startswith(b"<\x00"):
        return "utf-16-le"
    if data.startswith(b"\x00<"):
        return "utf-16-be"
    return None


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _attr(el: ElementTree.Element, key: str) -> str | None:
    """An attribute by local name, whatever its namespace."""
    found = el.get(key)
    if found is not None:
        return found
    for k, v in el.attrib.items():
        if _local(k) == key:
            return v
    return None


def read_docx(name: str, raw: bytes, sink: Sink) -> None:
    what = "Word document"
    budget = _Budget(name)
    with _open_zip(name, raw, what) as zf:
        data = _read_member(zf, "word/document.xml", name, what, budget)
    if data is None:
        raise unreadable(name, what)
    depth = 0  # inside <body>
    paragraphs: list[list[str]] = []  # open paragraphs, innermost last
    rows: list[list[str]] = []  # open table rows' cells
    cells: list[list[str]] = []  # open table cells' paragraphs
    for event, tag, el in _events(data, name, what, budget):
        if event == "start":
            if tag == "body":
                depth += 1
            elif depth:
                if tag == "p":
                    paragraphs.append([])
                elif tag == "tr":
                    rows.append([])
                elif tag == "tc":
                    cells.append([])
            continue
        if tag == "body":
            depth -= 1
        if not depth:
            continue
        if paragraphs:
            if tag == "t" and el.text:
                paragraphs[-1].append(el.text)
            elif tag == "tab":
                paragraphs[-1].append("\t")
            elif tag in ("br", "cr"):
                paragraphs[-1].append("\n")
        if tag == "p" and paragraphs:
            text = "".join(paragraphs.pop())
            if cells:
                cells[-1].append(text)
            else:
                sink.add(text)
        elif tag == "tc" and cells:
            text = " ".join(cells.pop()).strip()
            if rows:
                rows[-1].append(text)
        elif tag == "tr" and rows:
            sink.add("\t".join(rows.pop()))


def read_xlsx(name: str, raw: bytes, sink: Sink) -> int:
    what = "spreadsheet"
    budget = _Budget(name)
    with _open_zip(name, raw, what) as zf:
        shared = _shared_strings(zf, name, budget)
        sheets = _sheet_parts(zf, name, budget)
        for sheet_name, part in sheets:
            sink.add(f"[sheet: {sheet_name}]", marker=True)
            data = _read_member(zf, part, name, what, budget)
            if data is not None:
                _sheet(data, name, shared, sink, budget)
    return len(sheets)


def _sheet(data: bytes, name: str, shared: list[str], sink: Sink, budget: _Budget) -> None:
    row: _Row | None = None
    cell: _Cell | None = None
    for event, tag, el in _events(data, name, "spreadsheet", budget):
        if event == "start":
            if tag == "row":
                row = _Row()
            elif tag == "c" and row is not None:
                cell = _Cell(_attr(el, "r"), _attr(el, "t"))
            elif tag == "is" and cell is not None:
                cell.inline_depth += 1
            elif tag == "rPh" and cell is not None:
                cell.phonetic_depth += 1
            continue
        if cell is not None:
            if tag == "rPh":
                cell.phonetic_depth -= 1
            elif tag == "t" and cell.inline_depth and not cell.phonetic_depth:
                cell.inline.append(el.text or "")
            elif tag == "v":
                cell.value = el.text or ""
            elif tag == "is":
                cell.inline_depth -= 1
            elif tag == "c" and row is not None:
                row.add(cell.ref, cell.text(shared))
                cell = None
        if tag == "row" and row is not None:
            line = row.line()
            if line:
                sink.add(line)
            row = None


# Excel's last column, XFD.
MAX_COLUMNS = 16_384


class _Row:
    """One row's non-empty cells by column; blanks are filled only on output."""

    def __init__(self) -> None:
        self._cells: dict[int, str] = {}
        self._next = 0

    def add(self, ref: str | None, text: str) -> None:
        col = _column_index(ref)
        # No reference, or one past XFD: the cell after the last one.
        if col is None or col >= MAX_COLUMNS:
            col = self._next
        self._next = col + 1
        if text:
            # A line break inside a cell would read as a new row.
            self._cells[col] = " ".join(text.split("\n"))

    def line(self) -> str:
        """Tab-separated, blanks kept so column C stays third.

        Padding is only ever written up to a cell with text, so its cost is
        tabs the sink counts against the cap: an empty far-right cell (XFD1)
        costs nothing.
        """
        if not self._cells:
            return ""
        last = max(self._cells)
        return "\t".join(self._cells.get(i, "") for i in range(last + 1)).rstrip("\t")


class _Cell:
    def __init__(self, ref: str | None, kind: str | None) -> None:
        self.ref = ref
        self.kind = kind
        self.value = ""
        self.inline: list[str] = []
        self.inline_depth = 0
        self.phonetic_depth = 0

    def text(self, shared: list[str]) -> str:
        if self.kind == "inlineStr":
            return "".join(self.inline)
        raw = self.value
        if self.kind == "s":
            try:
                return shared[int(raw)]
            except (ValueError, IndexError):
                return ""
        if self.kind == "b":
            return "TRUE" if raw == "1" else "FALSE" if raw == "0" else raw
        return raw


def _shared_strings(zf: zipfile.ZipFile, name: str, budget: _Budget) -> list[str]:
    data = _read_member(zf, "xl/sharedStrings.xml", name, "spreadsheet", budget)
    if data is None:
        return []
    out: list[str] = []
    current: list[str] | None = None
    # A phonetic run (<rPh>, furigana) carries its own <t>: a reading aid,
    # not part of the cell's text.
    phonetic = 0
    for event, tag, el in _events(data, name, "spreadsheet", budget):
        if tag == "rPh":
            phonetic += 1 if event == "start" else -1
            continue
        if tag != "si" and tag != "t":
            continue
        if event == "start":
            if tag == "si":
                current = []
        elif tag == "t" and current is not None and not phonetic:
            current.append(el.text or "")
        elif tag == "si" and current is not None:
            out.append("".join(current))
            current = None
    return out


def _sheet_parts(zf: zipfile.ZipFile, name: str, budget: _Budget) -> list[tuple[str, str]]:
    """(sheet name, zip member) in workbook order."""
    what = "spreadsheet"
    book = _read_member(zf, "xl/workbook.xml", name, what, budget)
    if book is None:
        raise unreadable(name, what)
    targets: dict[str, str] = {}
    rels = _read_member(zf, "xl/_rels/workbook.xml.rels", name, what, budget)
    if rels is not None:
        for event, tag, el in _events(rels, name, what, budget):
            if event == "end" and tag == "Relationship":
                target = (_attr(el, "Target") or "").lstrip("/")
                if not target.startswith("xl/"):
                    target = f"xl/{target}"
                targets[_attr(el, "Id") or ""] = target
    out: list[tuple[str, str]] = []
    in_sheets = 0
    for event, tag, el in _events(book, name, what, budget):
        if tag == "sheets":
            in_sheets += 1 if event == "start" else -1
        elif tag == "sheet" and event == "end" and in_sheets:
            i = len(out) + 1
            part = targets.get(_attr(el, "id") or "", f"xl/worksheets/sheet{i}.xml")
            out.append((_attr(el, "name") or f"Sheet{i}", part))
    return out


_COLUMN = re.compile(r"[A-Z]+")


def _column_index(ref: str | None) -> int | None:
    if not ref:
        return None
    m = _COLUMN.match(ref)
    if m is None or len(m.group(0)) > 3:
        return None
    index = 0
    for ch in m.group(0):
        index = index * 26 + (ord(ch) - 64)
    return index - 1
