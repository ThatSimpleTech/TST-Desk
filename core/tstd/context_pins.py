"""Workspace context pins (TD-2804).

``.tst/context/pins.yaml`` is git-tracked (not gitignored). The human
pins; the assembler reads. Unpin removes the pin, not the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .context.tokens import heuristic_count
from .logging import get_logger

log = get_logger("tstd.context_pins")

PINS_RELATIVE = Path(".tst") / "context" / "pins.yaml"


class PinOutsideError(ValueError):
    """The path is outside the workspace wall."""


class PinRecord(BaseModel):
    path: str = Field(min_length=1)
    kind: Literal["file", "dir"]
    added_at: str


class PinCard(BaseModel):
    path: str
    name: str
    kind: Literal["file", "dir"]
    lines: int = Field(ge=0)


def pins_file(workspace: Path) -> Path:
    return workspace / PINS_RELATIVE


def load_pins(workspace: Path) -> list[PinRecord]:
    path = pins_file(workspace)
    if not path.is_file():
        return []
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning(
            "pins.yaml unreadable",
            extra={"extra_fields": {"path": str(path), "error": str(exc)}},
        )
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("pins")
    if not isinstance(raw, list):
        return []
    out: list[PinRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(PinRecord.model_validate(item))
        except Exception:
            continue
    return out


def save_pins(workspace: Path, pins: list[PinRecord]) -> None:
    path = pins_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pins": [p.model_dump() for p in pins]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def resolve_inside(workspace: Path, raw: str) -> Path:
    """Resolve *raw* and refuse anything outside *workspace*."""
    root = workspace.resolve()
    candidate = Path(raw)
    target = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise PinOutsideError(f"path outside the workspace: {raw}") from exc
    if resolved != root and root not in resolved.parents:
        raise PinOutsideError(f"path outside the workspace: {raw}")
    if resolved == root:
        raise PinOutsideError("cannot pin the workspace root")
    return resolved


def _relative(workspace: Path, target: Path) -> str:
    return target.relative_to(workspace.resolve()).as_posix()


def _line_count(path: Path) -> int:
    if path.is_file():
        try:
            return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return 0
    if not path.is_dir():
        return 0
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += _line_count(child)
    except OSError:
        return total
    return total


def list_pin_cards(workspace: Path) -> list[PinCard]:
    root = workspace.resolve()
    cards: list[PinCard] = []
    for pin in load_pins(workspace):
        target = root / pin.path
        kind = pin.kind
        if target.is_dir():
            kind = "dir"
        elif target.is_file():
            kind = "file"
        cards.append(
            PinCard(
                path=pin.path,
                name=Path(pin.path).name,
                kind=kind,
                lines=_line_count(target) if target.exists() else 0,
            )
        )
    return cards


def add_pin(workspace: Path, raw: str) -> PinRecord:
    target = resolve_inside(workspace, raw)
    rel = _relative(workspace, target)
    kind: Literal["file", "dir"] = "dir" if target.is_dir() else "file"
    pins = [p for p in load_pins(workspace) if p.path != rel]
    record = PinRecord(
        path=rel,
        kind=kind,
        added_at=datetime.now(UTC).isoformat(),
    )
    pins.append(record)
    save_pins(workspace, pins)
    return record


def remove_pin(workspace: Path, rel: str) -> None:
    pins = [p for p in load_pins(workspace) if p.path != rel]
    save_pins(workspace, pins)


@dataclass(frozen=True)
class ProjectContextLoad:
    """Brain-only pin block after LIFO budget (TD-2805)."""

    files: tuple[PinRecord, ...]
    dropped: tuple[PinRecord, ...]
    block: str | None
    tokens: int


def _pin_body(workspace: Path, pin: PinRecord) -> str:
    target = workspace.resolve() / pin.path
    if target.is_file():
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    if target.is_dir():
        try:
            names = sorted(p.name for p in target.iterdir() if p.is_file())
        except OSError:
            return ""
        return "folder: " + ", ".join(names)
    return ""


def load_project_context(workspace: Path, token_budget: int) -> ProjectContextLoad:
    """Load pins oldest-first; drop newest until under *token_budget*."""
    ordered = sorted(load_pins(workspace), key=lambda p: p.added_at)
    kept: list[PinRecord] = list(ordered)
    dropped: list[PinRecord] = []

    def _block(pins: list[PinRecord]) -> str:
        parts = ["<!-- project_context -->"]
        for pin in pins:
            parts.append(f"### {pin.path}\n{_pin_body(workspace, pin)}")
        return "\n\n".join(parts)

    while kept:
        text = _block(kept)
        tokens = heuristic_count(text).count
        if tokens <= token_budget:
            return ProjectContextLoad(tuple(kept), tuple(dropped), text, tokens)
        dropped.insert(0, kept.pop())
    return ProjectContextLoad((), tuple(dropped), None, 0)


def project_capacity(
    workspace: Path,
    token_budget: int,
) -> tuple[int, int, int, list[str]]:
    """Instructions + memory + pins vs cap. Returns tokens and dropped names."""
    from .context.memory_loader import list_workspace_memory

    instruction_text = ""
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = workspace / name
        if path.is_file():
            instruction_text += path.read_text(encoding="utf-8", errors="replace")
            break
    rules = workspace / ".tst" / "rules"
    if rules.is_dir():
        for rule in sorted(rules.glob("*.md")):
            instruction_text += rule.read_text(encoding="utf-8", errors="replace")
    instruction_tokens = heuristic_count(instruction_text).count if instruction_text else 0
    memory_tokens = 0
    for item in list_workspace_memory(workspace):
        memory_tokens += heuristic_count(item.content).count
    loaded = load_project_context(workspace, token_budget)
    return instruction_tokens, memory_tokens, loaded.tokens, [p.path for p in loaded.dropped]
