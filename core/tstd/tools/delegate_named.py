"""``delegate`` by name: named worker definitions (TD-5133).

The definitions themselves are read by :mod:`tstd.context.workers`. This
module is what ``delegate`` and the loop do with them: pick one by name,
work out its tier and tools, tell the model which workers exist, and
point a turn at the worker its message names as ``@name``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..config import ModelConfig, TierConfig, TierName
from ..context.workers import WorkerCatalog, WorkerDefinition, load_workers_async
from ..provider import FunctionDefinition, MessageContent
from ..provider import ToolDefinition as ProviderToolDefinition
from .delegate_child import CONSULT_TOOL_NAME, SUMMARY_TOKEN_CAP
from .results import HandlerRefusal

#: The delegate argument that names a worker.
AGENT_ARG = "agent"


async def resolve_named_worker(
    workspace: str, agent: str, *, kind: str | None, plan_mode: bool
) -> WorkerDefinition:
    """The definition *agent* names, read fresh from disk, or a refusal.

    Read at call time, not from the turn's list, so a file the user just
    edited or removed is what runs or is refused.
    """
    if kind is not None:
        raise HandlerRefusal(
            "delegate_agent_kind",
            "Error: give either agent or kind, not both; a named worker sets its own tools.",
        )
    catalog = await load_workers_async(workspace)
    worker = catalog.get(agent)
    if worker is None:
        problem = next((p for p in catalog.problems if p.path.stem == agent), None)
        why = f" Its file was not loaded: {problem.reason}." if problem else ""
        known = ", ".join(catalog.names) or "none are defined"
        raise HandlerRefusal(
            "delegate_agent_unknown",
            f"Error: no named worker {agent!r}.{why} Workers: {known}.",
        )
    if plan_mode and worker.writes:
        raise HandlerRefusal(
            "delegate_agent_plan_mode",
            f"Refused: worker {agent!r} can write or run shell, and plan mode only runs "
            "read-only workers.",
        )
    return worker


def worker_tier(config: ModelConfig, worker: WorkerDefinition) -> tuple[TierName, TierConfig]:
    """The tier name and config *worker* runs on.

    A named ``preset`` that the config no longer has is refused rather
    than quietly run on the active preset.
    """
    if worker.preset is None:
        return worker.tier, config.tier(worker.tier)
    preset = config.presets.get(worker.preset)
    if preset is None:
        raise HandlerRefusal(
            "delegate_agent_preset",
            f"Error: worker {worker.name!r} names preset {worker.preset!r}, which is not "
            f"configured. Presets: {', '.join(sorted(config.presets))}.",
        )
    tier_cfg: TierConfig = getattr(preset, worker.tier)
    return worker.tier, tier_cfg


def named_tool_names(worker: WorkerDefinition, *, consult: bool) -> tuple[str, ...]:
    """*worker*'s allow list, less ``consult`` when its tier is not offered it."""
    return tuple(t for t in worker.tools if consult or t != CONSULT_TOOL_NAME)


def named_preamble(worker: WorkerDefinition, tools: list[str]) -> str:
    """The instruction ahead of the task for a named worker."""
    return (
        f"You are the named worker {worker.name!r}. Your tools are "
        f"{', '.join(tools) or 'none'}; you cannot call delegate. Follow your "
        "instructions in the system prompt, complete the task, then reply with "
        f"a concise summary, in under {SUMMARY_TOKEN_CAP} tokens, and no further "
        "tool calls."
    )


def named_system_prompt(assembled: str, worker: WorkerDefinition) -> str:
    """The worker tier's assembled prompt with the definition's own prompt after it."""
    if not worker.prompt:
        return assembled
    return f"{assembled}\n\n## Worker {worker.name}\n\n{worker.prompt}"


def with_named_workers(
    definitions: list[ProviderToolDefinition] | None, catalog: WorkerCatalog
) -> list[ProviderToolDefinition] | None:
    """*definitions* with ``delegate`` listing *catalog*'s workers.

    With no workers the list is returned as it is, so the request (and
    the provider's prompt cache) is unchanged for a workspace without
    definitions.
    """
    if definitions is None or not catalog.workers:
        return definitions
    out: list[ProviderToolDefinition] = []
    for d in definitions:
        if d.function is None or d.function.name != "delegate":
            out.append(d)
            continue
        out.append(replace(d, function=_listed(d.function, catalog)))
    return out


def _listed(fn: FunctionDefinition, catalog: WorkerCatalog) -> FunctionDefinition:
    lines = "\n".join(f"- {w.name}: {w.description}" for w in catalog.workers)
    description = (
        f"{fn.description}\n\nNamed workers (pass agent, not kind; each has its own "
        f"prompt, tools and model):\n{lines}"
    )
    parameters: dict[str, Any] = dict(fn.parameters)
    properties = dict(parameters.get("properties", {}))
    agent = dict(properties.get(AGENT_ARG, {"type": "string"}))
    agent["enum"] = list(catalog.names)
    properties[AGENT_ARG] = agent
    parameters["properties"] = properties
    return replace(fn, description=description, parameters=parameters)


def worker_mention_note(names: tuple[str, ...]) -> str:
    """What the model is told when the user's message names workers as ``@name``."""
    listed = ", ".join(f"@{n}" for n in names)
    calls = " ".join(f'delegate with agent "{n}".' for n in names)
    return f"[The user addressed this message to {listed}. Hand the task to it: call {calls}]"


def with_worker_mentions(content: MessageContent, names: tuple[str, ...]) -> MessageContent:
    """*content* with the ``@name`` note after it; unchanged when *names* is empty."""
    if not names:
        return content
    note = worker_mention_note(names)
    if isinstance(content, str):
        return f"{content}\n\n{note}"
    return [*content, {"type": "text", "text": note}]
