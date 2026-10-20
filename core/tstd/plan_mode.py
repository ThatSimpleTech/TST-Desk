"""What the model is told about read-only plan mode (TD-5123).

Plan mode lives on the router (TD-4603) and is judged by the classifier's
``plan-mode`` rule. This module only holds the words: the note a turn's
message carries while planning, and the refusal a call gets when plan
mode stops it. Kept free of session and daemon imports so the dispatcher
can use it.
"""

from __future__ import annotations

from .autonomy.classifier import DecisionRequest
from .provider import MessageContent

#: ``ToolResult.error_code`` of a call plan mode refused.
PLAN_MODE_REFUSED = "plan_mode"

PLAN_NOTE = (
    "[Plan mode is on. You are planning, not changing anything: writes, edits, "
    "shell commands outside the read-only list (ls, cat, grep, git status/log/diff "
    "and the like), delegate with kind worker, and tools not marked read-only are "
    "refused. Read, search and research what you need (delegate with kind research "
    "is fine), then call exit_plan_mode with your plan. The user approves, edits or "
    "rejects it; once approved, plan mode ends and you carry the plan out.]"
)

_NEXT = (
    "Read and research what you need, then call exit_plan_mode with your plan; once "
    "the user approves it, plan mode ends and you can make changes."
)


def with_plan_note(content: MessageContent) -> MessageContent:
    """*content* with the plan-mode note after it, as the model receives it.

    The note follows the person's own words, so their message stays
    first; the window shows the message as typed (``user_turn``).
    """
    if isinstance(content, str):
        return f"{content}\n\n{PLAN_NOTE}"
    return [*content, {"type": "text", "text": PLAN_NOTE}]


def _why(request: DecisionRequest) -> str:
    name = request.tool_name
    provenance = request.provenance or ""
    if provenance.startswith("mcp:"):
        server = provenance.removeprefix("mcp:")
        return (
            f"`{name}` is an MCP tool its server's config does not list as read-only "
            f"(mcp.servers.{server}.read_only_tools), so it could change something."
        )
    if provenance:
        return f"`{name}` comes from a plugin, and nothing marks it read-only."
    if name == "shell":
        return "`shell` runs only commands on the read-only list while planning."
    if name == "delegate":
        return "`delegate` with kind worker writes; use kind research while planning."
    if name == "propose_steering_edit":
        return "A steering edit is a change, even as a proposal."
    return f"`{name}` could change something."


def planning_refusal(request: DecisionRequest) -> str:
    """The tool result for a call plan mode refused: it tells the model it is planning."""
    return "Refused: plan mode is on, so you are planning, not changing anything. " + (
        f"{_why(request)} {_NEXT}"
    )
