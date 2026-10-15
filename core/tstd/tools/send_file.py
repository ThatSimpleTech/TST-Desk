"""The ``send_file`` tool: the agent sends files to the user in the chat (TD-5143).

Each file is copied into the session's data dir and shown as a card. The
dispatcher guards every path in ``files`` before the handler runs
(TD-5143 list path fields), and the classifier puts an in-workspace send
in Class A, so no approval card appears in any mode.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from ..logging import get_logger
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

if TYPE_CHECKING:
    from ..artifacts import ArtifactRecord
    from ..protocol import ArtifactReady
    from ..sent_files import SendGate, SentFiles

log = get_logger("tstd.tools.send_file")

SEND_FILE_DESCRIPTION = (
    "Send files to the user in this chat: a built APK, a PDF, an image, a zip, a report. "
    "Each file shows as a card the user can open or save on the computer, or download "
    "and open on their phone (an APK opens Android's installer). Use this whenever the "
    "user asks you to give, send, share or pass them a file, instead of telling them a "
    "path. Paths must be inside the workspace; copy a file in first if it is not. "
    "Folders are refused: zip them first. Up to 10 files per call. Each file is copied "
    "when you call this, so later changes to it are not sent."
)

SEND_FILE_TOOL = Tool(
    name="send_file",
    description=SEND_FILE_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 10,
                "description": (
                    "Paths of the files to send, relative to the workspace root "
                    "or absolute inside it."
                ),
            },
            "caption": {
                "type": "string",
                "maxLength": 500,
                "description": "Optional short note shown with the file cards.",
            },
        },
        "required": ["files"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=True,
    path_fields=("files",),
    mutates=False,
)

_UNAVAILABLE = "Sending files is not available in this session."


def register_send_file_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``send_file`` tool. Call from ``create_registry``."""
    registry.register(SEND_FILE_TOOL)


async def send_file(
    *,
    session: object,
    tool_call_id: str = "",
    files: list[str],
    caption: str | None = None,
    service: SentFiles | None,
) -> str:
    """Copy *files* into the session and emit one ``artifact_ready`` each.

    A cancel (Stop, a timeout, shutdown) cannot stop the copy thread, so the
    two settle on a ``SendGate``: cancelled before indexing, the thread undoes
    its copies and nothing is announced; cancelled once indexing began, the
    rows land and the cards are announced anyway. The index and the chat
    always agree.
    """
    # Imported here: sent_files -> artifacts -> tools.boundary loads this package.
    from ..sent_files import SendError, SendGate, result_text

    session_id = getattr(session, "id", None)
    persist_dir = getattr(session, "persist_dir", None)
    event_log = getattr(session, "event_log", None)
    workspace = getattr(session, "workspace_path", None)
    add = getattr(event_log, "add", None)
    if (
        service is None
        or not isinstance(session_id, str)
        or persist_dir is None
        or add is None
        or not workspace
    ):
        raise HandlerRefusal("send_unavailable", _UNAVAILABLE)
    gate = SendGate()
    copy = partial(
        service.copy_all,
        session_id,
        files,
        workspace=Path(workspace),
        caption=caption,
        tool_call_id=tool_call_id,
        gate=gate,
    )
    # Shielded: cancelling the handler never cancels the copy-then-announce task.
    sending = asyncio.ensure_future(_copy_and_announce(copy, gate, add))
    sending.add_done_callback(_retrieve)
    try:
        records = await asyncio.shield(sending)
    except asyncio.CancelledError:
        if not gate.cancel():
            # Indexing began first: let the cards land with the rows.
            with contextlib.suppress(SendError):
                await asyncio.shield(sending)
        raise
    except SendError as exc:
        raise HandlerRefusal(exc.code, exc.message) from None
    return result_text(records, records[0].caption if records else None)


async def _copy_and_announce(
    copy: Callable[[], list[ArtifactRecord]],
    gate: SendGate,
    add: Callable[[ArtifactReady], Awaitable[object]],
) -> list[ArtifactRecord]:
    from ..artifacts import artifact_ready_for

    try:
        records = await asyncio.to_thread(copy)
    except asyncio.CancelledError:
        gate.cancel()  # the event loop is going away; undo unless indexing began
        raise
    for record in records:
        await add(artifact_ready_for(record))
        log.info(
            "file sent",
            extra={
                "extra_fields": {"artifact_id": record.id, "size": record.size, "mime": record.mime}
            },
        )
    return records


def _retrieve(task: asyncio.Future[list[ArtifactRecord]]) -> None:
    """Mark the outcome seen: after a cancel, no one else awaits it."""
    if not task.cancelled():
        task.exception()
