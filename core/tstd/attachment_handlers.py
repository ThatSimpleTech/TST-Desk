"""``extract_attachment``: a staged document's chip, measured (TD-5131).

The composer cannot open a PDF, DOCX or XLSX, so it asks the daemon that
will take the message how much text is inside. The answer goes back on the
asking connection only (``attachment_extracted``, seq 1) and is not logged:
measuring a file is not a turn, and the extracted text itself never leaves
this function, only its size.

The answer is worked out off the connection's message pump
(:class:`Extractions`): the websocket server awaits each frame's handler
before it reads the next, so an inline extraction would leave the window
unable to interrupt, approve or send until the document was read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable

from .attachments import AttachmentError, AttachmentLimits, measure_document
from .context.tokens import CHARS_PER_TOKEN
from .protocol import AttachmentExtracted, ExtractAttachment

log = logging.getLogger(__name__)

# Documents read at once for chips, daemon-wide. Each one can hold a
# 10 MB file and a 64 MB XML part in memory; a window spamming requests
# waits its turn instead of multiplying that.
EXTRACT_SLOTS = 2


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


class Extractions:
    """``extract_attachment`` answers worked out in the background.

    :meth:`start` returns at once; the answer reaches *send* when the
    document has been read. :meth:`stop` cancels whatever is still waiting.
    """

    def __init__(self, slots: int = EXTRACT_SLOTS) -> None:
        self._slots = asyncio.Semaphore(slots)
        self._tasks: set[asyncio.Task[None]] = set()

    def start(
        self,
        msg: ExtractAttachment,
        limits: AttachmentLimits,
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        task = asyncio.create_task(self._answer(msg, limits, send), name="extract-attachment")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(
        self,
        msg: ExtractAttachment,
        limits: AttachmentLimits,
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        try:
            async with self._slots:
                reply = await extract_attachment(msg, limits)
        except Exception:
            # Never a silent chip stuck on "reading": say it failed.
            log.exception("extract_attachment failed")
            reply = AttachmentExtracted(
                session_id=msg.session_id,
                request_id=msg.request_id,
                name=msg.attachment.name,
                error_code="attachment_unreadable",
                message=f"{msg.attachment.name} couldn't be read. Nothing was sent.",
            ).model_dump_json()
        # The window may have gone while the document was read.
        with contextlib.suppress(Exception):
            await send(reply)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
