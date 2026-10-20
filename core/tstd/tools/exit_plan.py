"""The ``exit_plan_mode`` tool: put the plan in front of the user (TD-5123).

In read-only plan mode the model researches, then calls this with its
plan. The turn parks like an approval until the person approves, edits
or rejects it on the window or the phone. Approving ends plan mode and
pins the plan; rejecting keeps plan mode on and returns their feedback.
The classifier's ``exit-plan-mode`` rule puts the call in Class A, so the
review itself is the one prompt. Outside plan mode the call is refused.
"""

from __future__ import annotations

from ..logging import redact_secrets
from ..protocol import PLAN_MAX_CHARS
from .ask_user import denies_every_ask
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

EXIT_PLAN_DESCRIPTION = (
    "Only in plan mode: put your finished plan in front of the user and wait for their "
    "review. Write the plan in Markdown: the goal, the files you will change and how, and "
    "how you will check the result. The user approves, edits or rejects it. Approving ends "
    "plan mode, so you can make the changes; rejecting keeps plan mode on and tells you why. "
    "Outside plan mode it is refused: just do the work."
)

EXIT_PLAN_TOOL = Tool(
    name="exit_plan_mode",
    description=EXIT_PLAN_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "minLength": 1,
                "maxLength": PLAN_MAX_CHARS,
                "description": "The plan, in Markdown.",
            },
        },
        "required": ["plan"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)

_UNAVAILABLE = "Plan review is not available in this session."
_NOT_PLANNING = (
    "Plan mode is off, so there is no plan to approve. Carry on with the task; if the "
    "user wants to see a plan first, write it in your reply."
)
_UNATTENDED = (
    "No one is here to review a plan: this is an unattended run. End your turn with "
    "the plan in your reply."
)
_SECRET_SHAPED = (
    "The plan has a string shaped like a secret (a token prefix such as sk-, ghp_ or "
    "github_pat_ followed by a long run of letters and digits, an AWS key id, or a "
    "private-key header). Every window shows the logged plan with it blanked, so the user "
    "couldn't review what you wrote. Reword it and call exit_plan_mode again."
)


def register_exit_plan_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``exit_plan_mode``. Call from ``create_registry``."""
    registry.register(EXIT_PLAN_TOOL)


async def exit_plan_mode(*, session: object, tool_call_id: str = "", plan: str) -> str:
    """Park the turn on *plan* and return the person's review."""
    # Imported here: plan_review -> session -> tools would cycle.
    from ..plan_review import UNREVIEWED, format_reply, review
    from ..session import Session

    if not isinstance(session, Session) or session.router is None:
        raise HandlerRefusal("exit_plan_unavailable", _UNAVAILABLE)
    if not session.router.plan_mode:
        raise HandlerRefusal("plan_mode_off", _NOT_PLANNING)
    if session.autonomy or session.delegate_depth > 0 or denies_every_ask(session):
        raise HandlerRefusal("exit_plan_unattended", _UNATTENDED)
    text = plan.strip()
    if not text:
        raise HandlerRefusal("invalid_plan", "The plan is empty. Write it out and try again.")
    if redact_secrets(text) != text:
        raise HandlerRefusal("plan_secret_shaped", _SECRET_SHAPED)
    reply = await review(session, tool_call_id, text)
    if reply.outcome in UNREVIEWED:
        code, message = UNREVIEWED[reply.outcome]
        raise HandlerRefusal(code, message)
    return format_reply(reply, still_planning=session.router.plan_mode)
