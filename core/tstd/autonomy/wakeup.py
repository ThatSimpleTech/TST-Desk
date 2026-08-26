"""Wake-up summary for an unattended run (TD-4303).

When autonomy stops — definition of done, Class C, a cap, or a later
``breaker:`` trip — the session log and the notify channel get the same
summary: what changed, a ledger excerpt, refusals, and the auto branch.
Interactive sessions never emit this event.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..logging import get_logger, redact_secrets
from ..protocol import AutonomySummary
from .checkpoint import auto_branch
from .ledger import DecisionLedger, format_entry

if TYPE_CHECKING:
    from ..session import Session

log = get_logger("tstd.autonomy.wakeup")

LEDGER_RELATIVE_PATH = ".tst/autonomy/DECISIONS.md"
LEDGER_EXCERPT_CAP = 2000
CHANGED_PATH_CAP = 80
REFUSAL_CAP = 20
_GIT_TIMEOUT = 30.0
_LEDGER_ENTRIES = 3


async def deliver_wakeup(session: Session) -> None:
    """Emit ``autonomy_summary`` and notify, if this stop should wake the user.

    No-op for interactive sessions and for stop reasons ``should_notify``
    rejects. The notify callback still receives a single string — the
    formatted summary — so slack/ntfy keep ``send(config, message)``.
    """
    from .runner import should_notify

    if not session.autonomy:
        return
    reason = session.autonomy_stop_reason
    if reason is None or not should_notify(reason):
        return
    event = await build_wakeup_summary(session)
    stored = await session.event_log.add(event)
    if not isinstance(stored, AutonomySummary):
        return
    if session.autonomy_notify is not None:
        await session.autonomy_notify(format_notify_text(stored))


async def build_wakeup_summary(session: Session) -> AutonomySummary:
    """Assemble the wake-up event for *session*. Does not emit or notify."""
    reason = redact_secrets(session.autonomy_stop_reason or "")
    branch = _branch_name(session)
    workspace = Path(session.workspace_path)
    changed, excerpt = await asyncio.gather(
        asyncio.to_thread(_changed_paths, workspace, branch),
        asyncio.to_thread(_ledger_excerpt, workspace),
    )
    return AutonomySummary(
        session_id=session.id,
        reason=reason,
        branch=branch,
        ledger_path=LEDGER_RELATIVE_PATH,
        changed=changed,
        refusals=_refusals(session),
        ledger_excerpt=redact_secrets(excerpt),
        seq=1,
    )


def format_notify_text(summary: AutonomySummary) -> str:
    """Human body for slack/ntfy — richer than a one-line stop reason."""
    from .runner import DOD_MET

    prefix = "Autonomy complete" if summary.reason == DOD_MET else "Autonomy stopped"
    lines = [
        f"{prefix}: {summary.reason}",
        "",
        f"Branch: {summary.branch or '(none)'}",
        f"Ledger: {summary.ledger_path}",
        "",
        "Changed:",
    ]
    if summary.changed:
        lines.extend(f"- {path}" for path in summary.changed)
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("Refusals:")
    if summary.refusals:
        lines.extend(f"- {item}" for item in summary.refusals)
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("Ledger excerpt:")
    lines.append(summary.ledger_excerpt or "(none)")
    return "\n".join(lines)


def _branch_name(session: Session) -> str:
    charter = session.charter
    if charter is None:
        return ""
    try:
        return auto_branch(charter.slug)
    except ValueError:
        return ""


def _refusals(session: Session) -> list[str]:
    return [redact_secrets(item) for item in session.autonomy_refusals[:REFUSAL_CAP]]


def _ledger_excerpt(workspace: Path) -> str:
    try:
        entries = DecisionLedger(workspace).read()
    except (OSError, ValueError):
        return ""
    if not entries:
        return ""
    text = "\n\n".join(format_entry(entry) for entry in entries[-_LEDGER_ENTRIES:])
    if len(text) > LEDGER_EXCERPT_CAP:
        return text[: LEDGER_EXCERPT_CAP - 1] + "…"
    return text


def _changed_paths(workspace: Path, branch: str) -> list[str]:
    """Paths that differ on *branch* vs the merge-base with the default branch."""
    from ..tools.shell import sanitized_env

    if not branch:
        return []
    env = sanitized_env()
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR",
    ):
        env.pop(var, None)
    rc, _out = _git(workspace, env, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    if rc != 0:
        return []
    default = _default_branch(workspace, env)
    if default is None:
        return []
    rc, base = _git(workspace, env, "merge-base", default, branch)
    merge_base = base.strip()
    if rc != 0 or not merge_base:
        return []
    rc, out = _git(workspace, env, "diff", "--name-only", f"{merge_base}...{branch}")
    if rc != 0:
        return []
    paths = [line.strip() for line in out.splitlines() if line.strip()]
    return paths[:CHANGED_PATH_CAP]


def _default_branch(workspace: Path, env: dict[str, str]) -> str | None:
    for name in ("main", "master"):
        rc, _out = _git(workspace, env, "rev-parse", "--verify", "-q", f"refs/heads/{name}")
        if rc == 0:
            return name
    rc, out = _git(workspace, env, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    ref = out.strip()
    if rc == 0 and ref:
        return ref.removeprefix("refs/remotes/")
    return None


def _git(workspace: Path, env: dict[str, str], *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout
