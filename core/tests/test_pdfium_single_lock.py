"""One lock guards PDFium, whichever path calls it (TD-5130 + TD-5131).

Both stories call into ``pypdfium2`` from worker threads — ``fs_read`` for a
PDF the model reads, attachment extraction for a PDF the person attaches —
and each shipped with a lock of its own. Together that is one lock too many:
PDFium is not thread-safe, so the two could be inside the library at once.
"""

from __future__ import annotations

import threading

from tstd import attachment_extract, pdfium_lock
from tstd.tools import read_pdf


def test_both_pdf_paths_hold_the_same_lock() -> None:
    assert read_pdf.PDFIUM_LOCK is pdfium_lock.PDFIUM_LOCK
    assert attachment_extract.PDFIUM_LOCK is pdfium_lock.PDFIUM_LOCK


def test_holding_it_once_shuts_both_paths_out() -> None:
    with pdfium_lock.PDFIUM_LOCK:
        assert read_pdf.PDFIUM_LOCK.acquire(blocking=False) is False
        assert attachment_extract.PDFIUM_LOCK.acquire(blocking=False) is False


def test_neither_module_makes_a_lock_of_its_own() -> None:
    """A second ``threading.Lock()`` in either module is the bug coming back."""
    for module in (read_pdf, attachment_extract):
        locks = {
            id(value)
            for value in vars(module).values()
            if isinstance(value, type(threading.Lock()))
        }
        assert locks <= {id(pdfium_lock.PDFIUM_LOCK)}
