"""PDF reading for ``fs_read`` (TD-5130).

Text comes out page by page through PDFium (``pypdfium2``), which also
renders a page to pixels when a vision tier asks for page images. PDFium
is not thread-safe and ``fs_read`` runs in worker threads, several at once
when the model reads files in parallel, so every call into it holds
``PDFIUM_LOCK`` — the one in ``tstd.pdfium_lock``, shared with attachment
extraction, which calls into the same library from its own threads.

Page images are PNG, encoded here with zlib: the daemon has no imaging
library (Pillow ships only with the computer-use sidecar).
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..pdfium_lock import PDFIUM_LOCK
from .results import ToolImage

#: Pages read when the call names none.
PDF_DEFAULT_PAGES = 10
#: Most pages one call reads; a longer range is cut to this many.
PDF_MAX_PAGES = 20
#: Most page images one call renders, before the prompt's own image cap.
PDF_MAX_PAGE_IMAGES = 5
#: Longest side of a rendered page, in pixels. Larger gains a vision
#: model little and costs many more image tokens.
PAGE_IMAGE_LONG_SIDE = 1568
#: Highest render scale (2 = 144 dpi), so a small page is not blown up.
_MAX_SCALE = 2.0

_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


class PagesError(ValueError):
    """A ``pages`` argument that names no pages this PDF has."""


@dataclass
class PdfRead:
    """What one PDF read produced."""

    page_count: int
    first: int
    last: int
    #: (page number, text) for each page read, text with ``\\n`` breaks.
    texts: list[tuple[int, str]] = field(default_factory=list)
    images: list[ToolImage] = field(default_factory=list)
    #: Pages that were imaged; the rest of the range were not.
    imaged: list[int] = field(default_factory=list)
    #: Pages whose image was over the size cap even at half scale.
    too_large: list[int] = field(default_factory=list)


def looks_like_pdf(head: bytes, path: Path) -> bool:
    """A PDF by its header. A ``.pdf`` may have junk before ``%PDF-``."""
    if head.lstrip()[:5] == b"%PDF-":
        return True
    return path.suffix.lower() == ".pdf" and b"%PDF-" in head


def parse_pages(spec: str, page_count: int) -> tuple[int, int]:
    """1-based inclusive range from ``"3"`` or ``"1-5"``, cut to the PDF.

    Empty means the first ``PDF_DEFAULT_PAGES``. A range longer than
    ``PDF_MAX_PAGES`` keeps its first ``PDF_MAX_PAGES`` pages; the caller
    says where to continue.
    """
    if not spec.strip():
        return 1, min(page_count, PDF_DEFAULT_PAGES)
    match = _RANGE.match(spec)
    if match is None:
        raise PagesError(f'pages must look like "3" or "1-5", not {spec!r}')
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if first < 1 or last < first:
        raise PagesError(f"pages {spec!r} is not a range of pages counted from 1")
    if first > page_count:
        raise PagesError(
            f"pages {spec!r} starts after the end: the PDF has {page_count} "
            f"page{'' if page_count == 1 else 's'}"
        )
    last = min(last, page_count, first + PDF_MAX_PAGES - 1)
    return first, last


def encode_png(width: int, height: int, stride: int, rgb: bytes) -> bytes:
    """An 8-bit RGB PNG of *rgb* rows, each *stride* bytes apart."""
    row = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        raw += rgb[y * stride : y * stride + row]

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _page_text(page: Any) -> str:
    textpage = page.get_textpage()
    try:
        text = str(textpage.get_text_range())
    finally:
        textpage.close()
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def _render_png(page: Any, max_bytes: int) -> bytes | None:
    """The page as PNG within *max_bytes*, trying half scale once."""
    width, height = page.get_size()
    scale = min(_MAX_SCALE, PAGE_IMAGE_LONG_SIDE / max(width, height, 1.0))
    for attempt in (scale, scale / 2):
        bitmap = page.render(scale=attempt, rev_byteorder=True)
        try:
            if bitmap.n_channels != 3:
                return None
            png = encode_png(bitmap.width, bitmap.height, bitmap.stride, bytes(bitmap.buffer))
        finally:
            bitmap.close()
        if len(png) <= max_bytes:
            return png
    return None


def read_pdf(
    target: Path,
    pages: str,
    *,
    image_slots: int,
    max_image_bytes: int,
) -> PdfRead:
    """Text of the chosen pages, and images of the first *image_slots*.

    Raises ``PagesError`` for a bad range and ``OSError`` for a file
    PDFium cannot open (damaged, or locked with a password). Blocking:
    call from a worker thread.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # a build without the wheel
        raise OSError("PDF support is not installed in this build (pypdfium2)") from e

    with PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(str(target))
        except pdfium.PdfiumError as e:
            raise OSError(str(e)) from e
        try:
            count = len(pdf)
            if count == 0:
                return PdfRead(page_count=0, first=0, last=0)
            first, last = parse_pages(pages, count)
            result = PdfRead(page_count=count, first=first, last=last)
            slots = min(image_slots, PDF_MAX_PAGE_IMAGES)
            for number in range(first, last + 1):
                page = pdf[number - 1]
                try:
                    result.texts.append((number, _page_text(page)))
                    if len(result.imaged) + len(result.too_large) < slots:
                        png = _render_png(page, max_image_bytes)
                        if png is None:
                            result.too_large.append(number)
                        else:
                            result.images.append(ToolImage("image/png", png))
                            result.imaged.append(number)
                finally:
                    page.close()
            return result
        finally:
            pdf.close()
