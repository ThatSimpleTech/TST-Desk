"""Decisions ledger (TD-704) — ``.tst/autonomy/DECISIONS.md``.

Class A and B decisions append to the ledger in the spec §12.3 format,
human-readable markdown that parses back into structured entries.  The
append is atomic and safe under concurrent sessions (file lock + append
mode).  Every entry names a revertable commit; an action that cannot be
attributed to a commit is **not** Class A.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

try:  # POSIX advisory locks; Windows falls back to append-mode atomicity.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

DecisionClassT = Literal["A", "B", "C"]

# Workspace-relative ledger location (spec §12.3).
_LEDGER_REL = Path(".tst") / "autonomy" / "DECISIONS.md"


@dataclass(frozen=True)
class LedgerEntry:
    """One decision appended to the ledger (spec §12.3).

    Attributes:
        decision_class: ``"A"`` or ``"B"`` (C violations are refusals,
            not decisions the agent records for itself).
        what: What was chosen.
        why: Why it was chosen.
        commit: The revertable commit SHA the action is attributed to.
            Class A requires one (AC 3).
        timestamp: ISO-8601 UTC timestamp.
    """

    decision_class: DecisionClassT
    what: str
    why: str
    commit: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def undo_command(self) -> str | None:
        """The ``git revert`` command, when a commit is named."""
        return f"git revert {self.commit}" if self.commit else None


# ── Markdown format (spec §12.3) ───────────────────────────────────────


def format_entry(entry: LedgerEntry) -> str:
    """Render *entry* as a §12.3 markdown block."""
    ts = entry.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    commit_part = f" · commit {entry.commit}" if entry.commit else ""
    lines = [
        f"## {ts} · Class {entry.decision_class}{commit_part}",
        f"**Chose:** {entry.what}",
        f"**Why:** {entry.why}",
    ]
    if entry.undo_command:
        lines.append(f"**Undo:** `{entry.undo_command}`")
    return "\n".join(lines)


def parse_ledger(text: str) -> list[LedgerEntry]:
    """Parse ledger markdown back into structured entries.

    Round-trips ``format_entry`` output and tolerates the spec §12.3
    example exactly.
    """
    entries: list[LedgerEntry] = []
    current: dict[str, str] = {}

    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                entries.append(_entry_from_block(current))
            current = {"_header": line[3:].strip()}
        elif current and line.startswith("**Chose:** "):
            current["what"] = line[len("**Chose:** ") :]
        elif current and line.startswith("**Why:** "):
            current["why"] = line[len("**Why:** ") :]
        elif current and line.startswith("**Undo:** `"):
            current["undo"] = line[len("**Undo:** `") : -1]
    if current:
        entries.append(_entry_from_block(current))
    return entries


def _entry_from_block(block: dict[str, str]) -> LedgerEntry:
    header = block["_header"]
    ts_text, _, rest = header.partition(" · ")
    timestamp = datetime.strptime(ts_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    cls_text, _, commit_text = rest.partition(" · ")
    decision_class = cls_text.removeprefix("Class ").strip()
    if decision_class not in {"A", "B", "C"}:
        raise ValueError(f"unknown decision class in ledger: {decision_class!r}")
    commit = None
    if commit_text.startswith("commit "):
        commit = commit_text[len("commit ") :].strip()
    return LedgerEntry(
        timestamp=timestamp,
        decision_class=decision_class,  # type: ignore[arg-type]
        what=block.get("what", ""),
        why=block.get("why", ""),
        commit=commit,
    )


# ── The ledger ─────────────────────────────────────────────────────────


class DecisionLedger:
    """Append-only decisions ledger for one workspace.

    Usage::

        ledger = DecisionLedger(workspace)
        await ledger.append(
            LedgerEntry(decision_class="A", what="...", why="...", commit="abc123")
        )
        entries = ledger.read()

    ``append`` is atomic and serialized across sessions (advisory file
    lock on POSIX; append-mode atomicity on Windows), so concurrent
    sessions never interleave partial entries.
    """

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / _LEDGER_REL

    async def append(self, entry: LedgerEntry) -> None:
        """Append *entry* to the ledger (validated, atomic, locked).

        Raises:
            ValueError: If a Class A entry has no commit — an action
                that cannot be attributed to a commit is not Class A.
        """
        if entry.decision_class == "A" and entry.commit is None:
            raise ValueError("Class A decisions require a revertable commit")
        rendered = format_entry(entry) + "\n\n"
        await asyncio.to_thread(self._locked_append, rendered)

    def read(self) -> list[LedgerEntry]:
        """Return all entries in the ledger file (empty when absent)."""
        if not self.path.exists():
            return []
        return parse_ledger(self.path.read_text(encoding="utf-8"))

    def _locked_append(self, rendered: str) -> None:
        """Append *rendered* under an advisory lock (POSIX)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            fd = f.fileno()
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                f.write(rendered)
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
