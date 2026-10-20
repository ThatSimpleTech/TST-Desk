"""``extract_attachment``: a staged document's chip, measured (TD-5131).

The composer cannot open a PDF, DOCX or XLSX, so it asks the daemon that
will take the message how much text is inside. The answer goes back on the
asking connection only (``attachment_extracted``, seq 1) and is not logged:
measuring a file is not a turn, and the extracted text itself never leaves
this function, only its size.
"""

from __future__ import annotations

import asyncio
import math

from .attachments import AttachmentError, AttachmentLimits, measure_document
from .context.tokens import CHARS_PER_TOKEN
from .protocol import AttachmentExtracted, ExtractAttachment


async def extract_attachment(msg: ExtractAttachment, limits: AttachmentLimits) -> str:
    """The ``attachment_extracted`` answer for *msg*, as wire JSON."""
    name = msg.attachment.name
    try:
        # PDFium and unzip are blocking work; keep them off the event loop.
        doc = await asyncio.to_thread(measure_document, msg.attachment, limits)
    except AttachmentError as e:
        return AttachmentExtracted(
            session_id=msg.session_id,
            request_id=msg.request_id,
            name=name,
            error_code=e.code,
            message=e.message,
        ).model_dump_json()
    text = doc.text or ""
    return AttachmentExtracted(
        session_id=msg.session_id,
        request_id=msg.request_id,
        name=doc.name,
        kind=doc.document,
        units=doc.units,
        text_bytes=doc.counted_bytes,
        tokens=math.ceil(len(text) / CHARS_PER_TOKEN),
    ).model_dump_json()
