"""Session-end memory distill on the worker tier (TD-2301).

Spec §5 mechanic 2: a cheap worker completion proposes create / replace
/ delete under ``.tst/memory/``. The proposal is a unified diff against
the files on disk. This module never writes — accept (E24) is the write
path, and it is not ``fs_write``.
"""

from __future__ import annotations

import asyncio
import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .config import ModelConfig
from .cost import CostTracker
from .logging import get_logger
from .memory_store import memory_dir
from .provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderError,
)

log = get_logger("tstd.memory_distill")

MemoryAction = Literal["create", "replace", "delete"]

# Must not drift from the classifier's _STEERING_BASENAMES (TD-4502):
# distill bypasses classifier/PathGuard entirely, so this set is the
# only thing keeping a distilled change from creating
# .tst/memory/SKILL.md — self-persisting prompt material.
_STEERING_BASENAMES = frozenset({"AGENTS.MD", "CLAUDE.MD", "SKILL.MD"})

DISTILL_SYSTEM_PROMPT = """\
You distill a TST Desk session into workspace memory files.
Return JSON only, no prose, no tool calls:
{"changes":[{"action":"create"|"replace"|"delete","path":"<file.md>","content":"..."}]}
path is a basename under .tst/memory/ (or .tst/memory/<file.md>).
content is the full file body for create and replace; omit it on delete.
Propose only durable facts. Return {"changes":[]} if nothing is worth keeping.
Never write AGENTS.md, CLAUDE.md, SKILL.md, or a path outside .tst/memory/.
"""


class DistillProvider(Protocol):
    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError: ...


@dataclass(frozen=True)
class DistillTurn:
    """One user or assistant turn from the session transcript."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class MemoryChange:
    """One proposed file change, already diffed against disk."""

    action: MemoryAction
    relative: Path
    before: str | None
    after: str | None
    diff: str

    @property
    def name(self) -> str:
        return self.relative.name


@dataclass(frozen=True)
class DistillProposal:
    """Typed distill result. Empty ``changes`` means nothing to write."""

    changes: tuple[MemoryChange, ...]


async def distill_session(
    workspace: str | Path,
    turns: Sequence[DistillTurn],
    provider: DistillProvider,
    config: ModelConfig,
    tracker: CostTracker | None = None,
) -> DistillProposal | None:
    """Ask the worker to propose memory writes. Does not touch the disk.

    Returns ``None`` when the worker fails, returns unusable text, or
    proposes nothing that differs from disk. Cost, when a *tracker* is
    given, is a worker call off the current user turn.
    """
    on_disk = await asyncio.to_thread(_read_memory_files, Path(workspace))
    worker_cfg = config.tier("worker")
    request = ChatCompletionRequest(
        model=worker_cfg.require_slug(),
        messages=[
            ChatMessage(role="system", content=DISTILL_SYSTEM_PROMPT),
            ChatMessage(role="user", content=_user_payload(turns, on_disk)),
        ],
        tools=None,
        max_tokens=worker_cfg.max_output_tokens,
        temperature=0.0,
    )
    response = await provider.chat_completion(request)
    if isinstance(response, ProviderError):
        log.warning(
            "distill worker call failed",
            extra={"extra_fields": {"error": response.message}},
        )
        return None
    if response.usage is not None and tracker is not None:
        tracker.record_off_turn("worker", response.usage, worker_cfg)
    text = response.message.content or ""
    raw = _parse_changes(text)
    if raw is None:
        log.warning("distill output was not a typed proposal")
        return None
    changes = _against_disk(raw, on_disk)
    if not changes:
        return None
    return DistillProposal(tuple(changes))


def _read_memory_files(workspace: Path) -> dict[str, str]:
    root = memory_dir(workspace.resolve())
    found: dict[str, str] = {}
    try:
        if not root.is_dir():
            return found
        root = root.resolve()
    except OSError:
        return found
    for path in sorted(root.glob("*.md")):
        if path.name.upper() in _STEERING_BASENAMES:
            continue
        try:
            resolved = path.resolve()
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        found[path.name] = text
    return found


def _user_payload(turns: Sequence[DistillTurn], on_disk: dict[str, str]) -> str:
    parts = ["## Session"]
    if not turns:
        parts.append("(no completed turns)")
    for turn in turns:
        parts.append(f"### {turn.role}\n{turn.content}")
    parts.append("## Current memory")
    if not on_disk:
        parts.append("(empty)")
    for name, text in on_disk.items():
        parts.append(f"### .tst/memory/{name}\n{text}")
    return "\n\n".join(parts)


def _parse_changes(text: str) -> list[dict[str, object]] | None:
    payload = _extract_json(text)
    if not isinstance(payload, dict):
        return None
    rows = payload.get("changes")
    if not isinstance(rows, list):
        return None
    out: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        action = row.get("action")
        path = row.get("path")
        if action not in {"create", "replace", "delete"} or not isinstance(path, str):
            return None
        content = row.get("content")
        if action != "delete" and not isinstance(content, str):
            return None
        if action == "delete" and content is not None and not isinstance(content, str):
            return None
        parsed: dict[str, object] = {"action": action, "path": path}
        if isinstance(content, str):
            parsed["content"] = content
        out.append(parsed)
    return out


def _extract_json(text: str) -> object | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        stripped = "\n".join(body).strip()
        if stripped[:4].lower() == "json":
            stripped = stripped[4:].lstrip()
    try:
        parsed: object = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed


def _against_disk(
    raw: list[dict[str, object]],
    on_disk: dict[str, str],
) -> list[MemoryChange]:
    changes: list[MemoryChange] = []
    seen: set[str] = set()
    for row in raw:
        name = _memory_name(str(row["path"]))
        if name is None or name in seen:
            continue
        seen.add(name)
        exists = name in on_disk
        before = on_disk.get(name)
        action = str(row["action"])
        if action == "delete":
            if not exists or before is None:
                continue
            changes.append(
                MemoryChange(
                    action="delete",
                    relative=_relative(name),
                    before=before,
                    after=None,
                    diff=_unified_diff(before, "", name),
                )
            )
            continue
        after = str(row.get("content", ""))
        if exists and before == after:
            continue
        resolved: MemoryAction = "replace" if exists else "create"
        changes.append(
            MemoryChange(
                action=resolved,
                relative=_relative(name),
                before=before,
                after=after,
                diff=_unified_diff(before or "", after, name),
            )
        )
    return changes


def memory_basename(raw: str) -> str | None:
    """Return a ``.tst/memory/`` basename, or ``None`` if the path is refused."""
    return _memory_name(raw)


def _memory_name(raw: str) -> str | None:
    cleaned = raw.replace("\\", "/").strip()
    parts = Path(cleaned).parts
    if not parts or ".." in parts:
        return None
    name = parts[-1]
    if name.upper() in _STEERING_BASENAMES or not name.endswith(".md"):
        return None
    if len(parts) > 1 and parts != (".tst", "memory", name):
        return None
    return name


def _relative(name: str) -> Path:
    return Path(".tst") / "memory" / name


def _unified_diff(before: str, after: str, name: str) -> str:
    rel = f".tst/memory/{name}"
    old = _diff_lines(before)
    new = _diff_lines(after)
    return "".join(difflib.unified_diff(old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def _diff_lines(text: str) -> list[str]:
    if text == "":
        return []
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = f"{lines[-1]}\n"
    return lines
