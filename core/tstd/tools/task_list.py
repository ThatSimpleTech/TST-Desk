"""The ``task_list`` tool: the model keeps a task list for the user (TD-5122).

Each call sends the whole list and replaces the last one, so creating,
adding, reordering and ticking an item are all the same call. The list
lives on the session, not in the workspace: the classifier's
``task-list`` rule puts the call in Class A, so no approval card appears
in any mode.
"""

from __future__ import annotations

from pydantic import ValidationError

from ..protocol import TaskListItem
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

TASK_LIST_MAX_ITEMS = 30
TASK_CONTENT_MAX_CHARS = 200

TASK_LIST_DESCRIPTION = (
    "Keep a task list for work with three or more steps. The user sees it as your plan. "
    "Call it when you start, then each time a step starts or finishes. Send the whole list "
    "every time; it replaces the last one. Keep one item in_progress at a time and mark it "
    "done as soon as it is finished. Send an empty list to clear it. The list stays in view "
    "when older turns are summarized."
)

TASK_LIST_TOOL = Tool(
    name="task_list",
    description=TASK_LIST_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": TASK_LIST_MAX_ITEMS,
                "description": "The whole list, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": TASK_CONTENT_MAX_CHARS,
                            "description": "One short step, e.g. 'Fix the date parser'.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "done"],
                        },
                    },
                    "required": ["content", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["tasks"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)

_UNAVAILABLE = "A task list is not available in this session."


def register_task_list_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``task_list`` tool. Call from ``create_registry``."""
    registry.register(TASK_LIST_TOOL)


async def task_list(*, session: object, tool_call_id: str = "", tasks: list[dict[str, str]]) -> str:
    """Replace the session's task list with *tasks* and log it."""
    # Imported here: session -> tools would cycle.
    from ..session import Session
    from ..task_list import saved_text, set_task_list

    if not isinstance(session, Session):
        raise HandlerRefusal("task_list_unavailable", _UNAVAILABLE)
    try:
        # One line per item: a newline would break the numbered rendering.
        items = [
            TaskListItem.model_validate(
                {"content": " ".join(task["content"].split()), "status": task["status"]}
            )
            for task in tasks
        ]
    except ValidationError:
        raise HandlerRefusal(
            "task_list_invalid", "Every task needs some text. Nothing was saved."
        ) from None
    await set_task_list(session, items)
    return saved_text(items)
