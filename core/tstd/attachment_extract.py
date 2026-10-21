"""Text out of PDF, DOCX and XLSX attachments, on this machine (TD-5131).

A document attachment is not sent to the model as a file. The daemon turns
it into plain text here, with a marker line for every PDF page and every
XLSX sheet, and the text is what rides in the message, under the same caps
as an attached text file. The caps measure the *text*, not the file: a
6 MB PDF of scanned pages with 20 KB of text is fine, and a small XLSX whose
cells expand to more text than the cap is not (``attachments.py``).

No network, ever (directive 3). PDF goes through PDFium (``pypdfium2``),
which reads only the bytes handed to it. DOCX and XLSX are zip files of XML
and are read with the standard library alone (``attachment_office.py``,
which also holds the zip-bomb budget). Extraction stops as soon as the text
passes the caller's cap rather than finishing a million-page file only to
refuse it.

Deliberately leaf, like ``attachments.py``: nothing from ``tstd`` beyond
its ``attachment_*`` siblings and ``pdfium_lock`` (a leaf itself, shared
with ``fs_read``'s PDF path). Blocking: call from a worker thread.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Literal

from .attachment_office import MAX_ZIP_MEMBERS, read_docx, read_xlsx
from .attachment_text import DocumentError, Sink, TooLong, fmt, unreadable
from .pdfium_lock import PDFIUM_LOCK

__all__ = [
    "DocumentError",
    "DocumentKind",
    "ExtractedDocument",
    "extract_document",
    "sniff_document",
]

DocumentKind = Literal["pdf", "docx", "xlsx"]


@dataclass(frozen=True)
class ExtractedDocument:
    """The text of one document, and how many pages or sheets it held."""

    kind: DocumentKind
    text: str
    # Pages for a PDF, sheets for an XLSX, 0 for a DOCX (Word has no pages
    # until it lays the text out, so there is nothing honest to count).
    units: int


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
    sink = Sink(max_text_bytes)
    try:
        if kind == "pdf":
            units = _pdf(name, raw, sink)
        elif kind == "docx":
            read_docx(name, raw, sink)
            units = 0
        else:
            units = read_xlsx(name, raw, sink)
    except TooLong:
        raise DocumentError(
            "attachment_too_large",
            f"The text in {name} is longer than this workspace allows per file "
            f"({fmt(max_text_bytes)} of text). Attach a shorter document, or "
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


# --- PDF ---------------------------------------------------------------


def _pdf(name: str, raw: bytes, sink: Sink) -> int:
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
            raise unreadable(name, "PDF") from e
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
