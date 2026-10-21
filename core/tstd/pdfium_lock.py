"""The one lock around PDFium (TD-5130, TD-5131).

PDFium is not thread-safe, and two paths call into it from worker threads:
``fs_read`` reading a PDF (``tools/read_pdf.py``) and attachment text
extraction (``attachment_extract.py``). The two stories landed in the same
wave, each with a lock of its own, which is one lock too many: a tool read
and an attachment preview could then be inside the library at the same
time. Both import this one instead.

A leaf: nothing but the standard library.
"""

from __future__ import annotations

import threading

#: Held for the whole of any call into ``pypdfium2``, in either path.
PDFIUM_LOCK = threading.Lock()
