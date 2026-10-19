"""The ``propose_steering_edit`` tool (TD-5140).

The agent never writes a steering file. This tool takes the whole new
text, writes nothing, and puts a diff on a card; the user's Accept (or
Edit then Save) is the only thing that writes it, through the daemon.
The dispatcher guards ``path`` as a read before the handler runs, and the
classifier's ``steering-proposal`` rule puts the call in Class A, so the
card is the one approval.
"""

from __future__ import annotations

from ..steering_proposals import STEERING_TEXT_CAP
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

PROPOSE_STEERING_DESCRIPTION = (
    "Propose a change to a steering file: AGENTS.md, CLAUDE.md, a .tst/rules/*.md rule, a "
    "SKILL.md, or a slash-command file (.tst/commands/*.md). fs_write and fs_edit refuse "
    "these files; this tool writes nothing either. It shows the user a diff, and only the "
    "user's Accept writes the file. Pass the whole new file text, not a patch: read the file "
    "first and keep what should stay. Use it when the user asks you to add something to the "
    "project instructions ('add this to AGENTS.md', 'remember in this repo to always …'), "
    "and to draft a starter AGENTS.md: scan the repo first (README, build and test commands, "
    "CI config, layout) and propose a short file. Don't retry or write the file another way "
    "while the user decides. The charter and .tst/config.yaml can't be proposed."
)

PROPOSE_STEERING_TOOL = Tool(
    name="propose_steering_edit",
    description=PROPOSE_STEERING_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The steering file, relative to the workspace root (AGENTS.md, "
                    ".tst/rules/testing.md) or absolute inside it. It may not exist yet."
                ),
            },
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": STEERING_TEXT_CAP,
                "description": "The complete new content of the file.",
            },
        },
        "required": ["path", "text"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    path_fields=("path",),
    mutates=False,
)

_UNAVAILABLE = "Proposing steering edits is not available in this session."


def register_steering_edit_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``propose_steering_edit``. Call from ``create_registry``."""
    registry.register(PROPOSE_STEERING_TOOL)


async def propose_steering_edit(
    *,
    session: object,
    tool_call_id: str = "",
    path: str,
    text: str,
) -> str:
    """Park the proposal and emit ``steering_proposal``. Writes nothing."""
    # Imported here: steering_proposals -> session -> tools would cycle.
    from ..session import Session
    from ..steering_proposals import SteeringRefusal, propose

    if not isinstance(session, Session) or not session.workspace_path:
        raise HandlerRefusal("steering_unavailable", _UNAVAILABLE)
    try:
        parked = await propose(session, path, text, tool_call_id)
    except SteeringRefusal as refusal:
        raise HandlerRefusal(refusal.code, refusal.message) from None
    verb = "create" if parked.before is None else "change"
    waiting = (
        "This is an unattended run: the proposal waits for the user in the run summary."
        if parked.autonomy
        else "The user accepts, edits or rejects it on the card."
    )
    return (
        f"Proposed to {verb} {parked.relative} (proposal {parked.proposal_id}). "
        f"Nothing was written. {waiting} Carry on with the task."
    )
