"""Named worker definitions (TD-5133).

Human-authored markdown under ``.tst/agents/`` (the workspace) and
``~/.tstdesk/agents/`` (the user). Each file defines one worker that
``delegate`` can run by name: YAML frontmatter for ``name``,
``description``, ``tools``, ``tier`` or ``preset`` and ``policy``, and
the body as the worker's own prompt.

The agent never writes these trees (the classifier refuses them like
steering, prime directive 4). A definition can only narrow what a
built-in child already has: its tools are a subset of ONE built-in kind
(the worker kind's, or the research kind's) plus ``consult``, so no
named worker holds both ``shell``/writes and the web; and its policy
rules are ``ask`` or ``never``. Workspace files load without a trust
card, so a workspace definition always runs on the worker tier of the
active preset: only a user-global file may name another ``tier`` or a
``preset``. A file that breaks any of this is not loaded, and the
reason is kept so the caller can say why. User-global wins on the same
name, as for slash commands, so a cloned repo cannot replace a worker
the user wrote.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import TIER_NAMES, TierName
from ..policy import WorkerPolicyRule
from ..tools.delegate_child import (
    CONSULT_TOOL_NAME,
    RESEARCH_CHILD_TOOLS,
    WORKER_CHILD_TOOLS,
)
from .frontmatter import parse_frontmatter

WorkerSource = Literal["workspace", "user"]

#: The largest prompt body a definition may carry, as for slash commands.
WORKER_PROMPT_CAP = 32 * 1024
#: The longest description offered to the model for one worker.
WORKER_DESCRIPTION_CAP = 300

#: Every tool a named worker may list: what the two built-in kinds have,
#: plus ``consult``. Never ``delegate`` (no grandchildren), never a plugin
#: or MCP tool (nothing vouches for it here). One definition may only use
#: one kind's share of this list (:data:`WORKER_KIND_TOOLS`).
WORKER_ALLOWED_TOOLS: tuple[str, ...] = tuple(
    dict.fromkeys((*WORKER_CHILD_TOOLS, *RESEARCH_CHILD_TOOLS, CONSULT_TOOL_NAME))
)
#: The built-in kinds a named worker's tools must fit inside, one of them:
#: the worker kind writes and runs shell but has no web, the research kind
#: has the web but cannot write (TD-5117). ``consult`` goes with either.
WORKER_KIND_TOOLS: dict[str, frozenset[str]] = {
    "worker": frozenset((*WORKER_CHILD_TOOLS, CONSULT_TOOL_NAME)),
    "research": frozenset((*RESEARCH_CHILD_TOOLS, CONSULT_TOOL_NAME)),
}
#: Tools that can change something. A worker holding one does not run in
#: plan mode (TD-5123).
WORKER_WRITING_TOOLS = frozenset({"fs_write", "fs_edit", "shell"})

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_-]{0,63})\b")
_KNOWN_KEYS = frozenset({"name", "description", "tools", "tier", "preset", "policy"})


@dataclass(frozen=True)
class WorkerDefinition:
    """One named worker.

    ``tools`` is the allow list, in the file's order. With no ``tools``
    key the worker is read-only (the research kind's tools). ``tier`` is
    the tier the worker runs on, taken from ``preset`` when one is named
    and from the active preset otherwise.
    """

    name: str
    description: str
    prompt: str
    tools: tuple[str, ...]
    tier: TierName
    preset: str | None
    policy: tuple[WorkerPolicyRule, ...]
    source: WorkerSource
    path: Path

    @property
    def writes(self) -> bool:
        """Whether the worker holds a tool that can change something."""
        return any(t in WORKER_WRITING_TOOLS for t in self.tools)


@dataclass(frozen=True)
class WorkerProblem:
    """A definition file that was not loaded, and why."""

    path: Path
    reason: str


@dataclass(frozen=True)
class WorkerCatalog:
    """The workers found for a workspace, by name, and the files refused."""

    workers: tuple[WorkerDefinition, ...] = ()
    problems: tuple[WorkerProblem, ...] = field(default_factory=tuple)

    def get(self, name: str) -> WorkerDefinition | None:
        """The worker called *name*, or ``None``."""
        return next((w for w in self.workers if w.name == name), None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(w.name for w in self.workers)


class WorkerDefinitionError(ValueError):
    """A definition file breaks the format; the message says how."""


def worker_dirs(workspace: str | Path, home_dir: str | Path | None = None) -> dict[str, Path]:
    """The two definition trees: the workspace's, then the user's."""
    home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
    return {
        "workspace": Path(workspace).expanduser() / ".tst" / "agents",
        "user": home / ".tstdesk" / "agents",
    }


def load_workers(workspace: str | Path, home_dir: str | Path | None = None) -> WorkerCatalog:
    """Read both trees; user-global wins on the same name. Sorted by name.

    Missing trees are not errors. Synchronous: wrap it in
    ``asyncio.to_thread`` on the I/O path (:func:`load_workers_async`).
    """
    found: dict[str, WorkerDefinition] = {}
    problems: list[WorkerProblem] = []
    dirs = worker_dirs(workspace, home_dir)
    for source in ("workspace", "user"):
        directory = dirs[source]
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".md"):
            try:
                text = path.read_text(encoding="utf-8")
                worker = parse_worker(text, path, "user" if source == "user" else "workspace")
            except (OSError, UnicodeDecodeError) as e:
                problems.append(WorkerProblem(path, f"could not be read: {e}"))
                continue
            except WorkerDefinitionError as e:
                problems.append(WorkerProblem(path, str(e)))
                continue
            found[worker.name] = worker
    return WorkerCatalog(
        workers=tuple(found[n] for n in sorted(found)),
        problems=tuple(problems),
    )


async def load_workers_async(
    workspace: str | Path, home_dir: str | Path | None = None
) -> WorkerCatalog:
    """:func:`load_workers` off the event loop."""
    return await asyncio.to_thread(load_workers, workspace, home_dir)


def parse_worker(text: str, path: Path, source: WorkerSource) -> WorkerDefinition:
    """Parse one definition file, or raise :class:`WorkerDefinitionError`."""
    metadata, body = parse_frontmatter(text)
    if not metadata:
        raise WorkerDefinitionError("has no YAML frontmatter")
    unknown = sorted(str(k) for k in metadata if k not in _KNOWN_KEYS)
    if unknown:
        # Fail closed: a key from another tool (a model slug, say) would
        # otherwise be silently ignored and the worker run differently.
        raise WorkerDefinitionError(f"has keys TST Desk does not read: {', '.join(unknown)}")
    name = metadata.get("name", path.stem)
    if not isinstance(name, str) or not _NAME.match(name):
        raise WorkerDefinitionError(
            "name must be 1-64 letters, digits, '-' or '_', starting with a letter or digit"
        )
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise WorkerDefinitionError("needs a description, so the model knows when to use it")
    prompt = body.strip()
    if len(prompt.encode("utf-8")) > WORKER_PROMPT_CAP:
        raise WorkerDefinitionError(f"prompt is over {WORKER_PROMPT_CAP} bytes")
    tier = _tier(metadata.get("tier"))
    preset = _preset(metadata.get("preset"))
    if source == "workspace" and (tier != "worker" or preset is not None):
        # Workspace files load without a trust card (a cloned repo's
        # included), so they may not move the child onto a dearer tier or
        # another preset's models. The user's own tree may.
        raise WorkerDefinitionError(
            "a workspace worker runs on the worker tier of the active preset; "
            "put it in ~/.tstdesk/agents/ to choose a tier or preset"
        )
    return WorkerDefinition(
        name=name,
        description=" ".join(description.split())[:WORKER_DESCRIPTION_CAP],
        prompt=prompt,
        tools=_tools(metadata.get("tools")),
        tier=tier,
        preset=preset,
        policy=_policy(metadata.get("policy"), name),
        source=source,
        path=path,
    )


def _tools(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return RESEARCH_CHILD_TOOLS
    if isinstance(raw, str):
        raw = [t for t in re.split(r"[,\s]+", raw) if t]
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise WorkerDefinitionError("tools must be a list of tool names")
    names = tuple(dict.fromkeys(t.strip() for t in raw))
    outside = [t for t in names if t not in WORKER_ALLOWED_TOOLS]
    if outside:
        raise WorkerDefinitionError(
            f"tools {', '.join(outside)} are not worker tools; "
            f"choose from {', '.join(WORKER_ALLOWED_TOOLS)}"
        )
    if not any(set(names) <= kind for kind in WORKER_KIND_TOOLS.values()):
        raise WorkerDefinitionError(
            "tools mix the worker kind's fs_write, fs_edit and shell with the research "
            "kind's web_search and web_fetch; a worker holds one kind's tools"
        )
    return names


def _tier(raw: Any) -> TierName:
    if raw is None:
        return "worker"
    for tier in TIER_NAMES:
        if raw == tier:
            return tier
    raise WorkerDefinitionError(f"tier must be one of {', '.join(TIER_NAMES)}")


def _preset(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise WorkerDefinitionError("preset must be a preset name")
    return raw.strip()


def _policy(raw: Any, worker: str) -> tuple[WorkerPolicyRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise WorkerDefinitionError("policy must be a list of rules")
    rules: list[WorkerPolicyRule] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str):
            raise WorkerDefinitionError("each policy rule needs a tool")
        effect = entry.get("effect")
        if effect not in ("ask", "never"):
            raise WorkerDefinitionError(
                "a worker's policy rule can only tighten: effect must be ask or never"
            )
        args = entry.get("args", "**")
        if not isinstance(args, str):
            raise WorkerDefinitionError("a policy rule's args must be a glob string")
        rules.append(WorkerPolicyRule(tool=entry["tool"], args=args, effect=effect, worker=worker))
    return tuple(rules)


def mentioned_workers(text: str, catalog: WorkerCatalog) -> tuple[str, ...]:
    """The defined workers *text* names as ``@name``, in order, once each."""
    known = set(catalog.names)
    return tuple(dict.fromkeys(m for m in _MENTION.findall(text) if m in known))
