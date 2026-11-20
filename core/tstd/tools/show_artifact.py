"""The ``show_artifact`` tool: the agent puts text in the Artifacts pane (TD-5254).

``send_file`` hands over a file as it is, as a card to save. This shows
something to read: a markdown document, a small HTML page, highlighted
code. The dispatcher guards ``path`` before the handler runs, and the
classifier puts the call in Class A by a rule of its own, because all it
writes is the session's own state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from ..logging import get_logger
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

if TYPE_CHECKING:
    from ..artifacts import ArtifactRecord
    from ..shown_artifacts import ShownArtifacts

log = get_logger("tstd.tools.show_artifact")

SHOW_ARTIFACT_DESCRIPTION = (
    "Show the user something to read in the Artifacts pane: a report, a plan, a table, a "
    "diagram, a small HTML page, a piece of code. Give path for a text file in the "
    "workspace; the pane shows the file as it is now, so later edits show too. Or give "
    "content and name for text that should not be a workspace file; name's extension "
    "picks the preview (.md, .html, or highlighted code). Calling again with the same "
    "path or name replaces what was shown. An HTML page is shown as a static picture of "
    "itself: no scripts, no links. Text only: to hand over a file as it is, use send_file."
)

SHOW_ARTIFACT_TOOL = Tool(
    name="show_artifact",
    description=SHOW_ARTIFACT_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": "Short name shown in the Artifacts pane.",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "A text file inside the workspace to show.",
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "The text to show, when it should not be a workspace file.",
            },
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": (
                    "File name with extension for content, such as summary.md or chart.html."
                ),
            },
            "caption": {
                "type": "string",
                "maxLength": 500,
                "description": "Optional short note shown with the artifact.",
            },
        },
        "required": ["title"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    # A second call for the same thing replaces the first: order matters.
    parallel_safe=False,
    path_fields=("path",),
    mutates=False,
)

_UNAVAILABLE = "Showing artifacts is not available in this session."


def register_show_artifact_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``show_artifact`` tool. Call from ``create_registry``."""
    registry.register(SHOW_ARTIFACT_TOOL)


async def show_artifact(
    *,
    session: object,
    tool_call_id: str = "",
    title: str,
    path: str | None = None,
    content: str | None = None,
    name: str | None = None,
    caption: str | None = None,
    service: ShownArtifacts | None,
) -> str:
    """Record the artifact and announce it with ``artifact_ready``.

    Recording and announcing run as one shielded step, so a Stop that
    lands between them cannot leave a row the window was never told of.
    """
    from ..artifacts import ArtifactError

    session_id = getattr(session, "id", None)
    workspace = getattr(session, "workspace_path", None)
    add = getattr(getattr(session, "event_log", None), "add", None)
    if service is None or not isinstance(session_id, str) or add is None or not workspace:
        raise HandlerRefusal("show_unavailable", _UNAVAILABLE)

    async def record_and_announce() -> ArtifactRecord:
        from ..artifacts import artifact_ready_for

        record = await asyncio.to_thread(
            service.show,
            session_id,
            Path(workspace),
            title=title,
            path=path,
            content=content,
            name=name,
            caption=caption,
            tool_call_id=tool_call_id,
        )
        await add(artifact_ready_for(record))
        log.info(
            "artifact shown",
            extra={"extra_fields": {"artifact_id": record.id, "mime": record.mime}},
        )
        return record

    task = asyncio.ensure_future(record_and_announce())
    task.add_done_callback(_retrieve)
    try:
        record = await asyncio.shield(task)
    except ArtifactError as exc:
        raise HandlerRefusal(exc.code, exc.message) from None
    what = record.name or record.title
    size = f", {record.size} bytes" if record.size is not None else ""
    return f'Showing "{record.title}" in Artifacts ({what}{size}).'


def _retrieve(task: asyncio.Future[ArtifactRecord]) -> None:
    """Mark the outcome seen: after a cancel, no one else awaits it."""
    if not task.cancelled():
        task.exception()
