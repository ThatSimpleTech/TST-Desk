"""The ``ask_user`` tool: structured questions for the person (TD-5121).

The model asks one to four questions, each with two to four options;
the person may also answer in their own words. The turn parks like an
approval until they answer on the window, the phone or ``tst run``; the
answers come back as the tool result. The classifier's ``ask-user`` rule
puts the call in Class A, so the question itself is the one prompt.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..logging import redact_secrets
from ..protocol import Question
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

ASK_USER_DESCRIPTION = (
    "Ask the user one to four short multiple-choice questions and wait for the answers. "
    "Use it when you need a decision only the user can make (which option, which file, "
    "what they prefer) and guessing would waste work. Each question has 2-4 options; the "
    "user can also type their own answer, so don't add an 'Other' option. Put the "
    "option you recommend first. Set multi_select when more than one option can apply. "
    "Don't use it for approval to run a tool: tool calls already ask for that. "
    "Not available in unattended runs."
)

_OPTION = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "The choice, in a few words. Shown on a button.",
        },
        "description": {
            "type": "string",
            "maxLength": 300,
            "description": "Optional: what picking it means.",
        },
    },
    "required": ["label"],
    "additionalProperties": False,
}

_QUESTION = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 1000},
        "header": {
            "type": "string",
            "maxLength": 40,
            "description": "Optional short tag for the question, e.g. 'Database'.",
        },
        "options": {"type": "array", "items": _OPTION, "minItems": 2, "maxItems": 4},
        "multi_select": {
            "type": "boolean",
            "description": "True when the user may pick more than one option.",
        },
    },
    "required": ["question", "options"],
    "additionalProperties": False,
}

ASK_USER_TOOL = Tool(
    name="ask_user",
    description=ASK_USER_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "questions": {"type": "array", "items": _QUESTION, "minItems": 1, "maxItems": 4},
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)

_UNAVAILABLE = "Asking the user is not available in this session."
_UNATTENDED = (
    "No one is here to answer: this is an unattended run. Decide with your best "
    "judgment, say what you assumed, and carry on."
)
_DENY_AND_CONTINUE = (
    "No one is here to answer: this run answers every question and approval with no "
    "(tst run --on-ask deny-and-continue). Decide with your best judgment, say what you "
    "assumed, and carry on."
)
#: Refused because the event log's secret scrub would change what the card shows.
SECRET_SHAPED = "ask_user_secret_shaped"
_SECRET_SHAPED_MESSAGE = (
    "Question {number}'s {part} has a string shaped like a secret (a long run of letters, "
    "digits, '-' or '_' after a token prefix such as sk-, ghp_ or github_pat_, an AWS key "
    "id, or a private-key header; ordinary words such as task-runner-integration can match "
    "too). The card would blank it, so the user couldn't see or pick it as you wrote it. "
    "Reword it and ask again."
)


def register_ask_user_tool(registry: ToolRegistry) -> None:
    """Register the builtin ``ask_user``. Call from ``create_registry``."""
    registry.register(ASK_USER_TOOL)


def hidden_part(question: Question) -> str | None:
    """Which part of *question* the event log's secret scrub would change, if any.

    Every window, phone and ``tst run`` draws the card from the logged
    ``question_request``, which the scrub has been through, while the
    parked question keeps the model's text. A label the scrub changes
    could never be picked (the answer would name the scrubbed label), two
    could collapse into one, and a scrubbed question or description is
    not what the model asked. So, as for a steering proposal (TD-5140),
    the question is refused rather than shown changed.
    """
    if redact_secrets(question.question) != question.question:
        return "question"
    if redact_secrets(question.header) != question.header:
        return "header"
    for number, option in enumerate(question.options, 1):
        if redact_secrets(option.label) != option.label:
            return f"option {number} label"
        if redact_secrets(option.description) != option.description:
            return f"option {number} description"
    return None


def parse_questions(raw: list[dict[str, Any]]) -> list[Question]:
    """The model's questions as protocol models.

    Refuses a repeated option, and any text the event log's secret scrub
    would change (see :func:`hidden_part`).
    """
    try:
        questions = [Question.model_validate(item) for item in raw]
    except ValidationError as e:
        raise HandlerRefusal("invalid_question", f"Invalid question: {e}") from None
    for number, question in enumerate(questions, 1):
        labels = [option.label.strip().casefold() for option in question.options]
        if len(set(labels)) != len(labels):
            raise HandlerRefusal("invalid_question", f"Question {number} repeats an option label.")
        part = hidden_part(question)
        if part is not None:
            raise HandlerRefusal(
                SECRET_SHAPED, _SECRET_SHAPED_MESSAGE.format(number=number, part=part)
            )
    return questions


def denies_every_ask(session: object) -> bool:
    """True in a ``tst run --on-ask deny-and-continue`` turn (TD-5118).

    That flag answers every approval with a refusal the model reads so
    the run never waits for a person, and a question is no different.
    ``Session.run_permissions`` (the turn's ``tst run`` rules) arrives
    with TD-5118, so it is read by name: before that lands no turn has
    any, and after it the refusal needs no further change here.
    """
    run = getattr(session, "run_permissions", None)
    return getattr(run, "on_ask", None) == "deny_and_continue"


async def ask_user(
    *,
    session: object,
    tool_call_id: str = "",
    questions: list[dict[str, Any]],
) -> str:
    """Park the turn on the questions and return the person's answers."""
    # Imported here: questions -> session -> tools would cycle.
    from ..questions import UNANSWERED, ask, format_reply
    from ..session import Session

    if not isinstance(session, Session):
        raise HandlerRefusal("ask_user_unavailable", _UNAVAILABLE)
    if session.autonomy or session.delegate_depth > 0:
        raise HandlerRefusal("ask_user_unattended", _UNATTENDED)
    if denies_every_ask(session):
        raise HandlerRefusal("ask_user_unattended", _DENY_AND_CONTINUE)
    parsed = parse_questions(questions)
    reply = await ask(session, tool_call_id, parsed)
    if reply.outcome in UNANSWERED:
        code, message = UNANSWERED[reply.outcome]
        raise HandlerRefusal(code, message)
    return format_reply(parsed, reply)
