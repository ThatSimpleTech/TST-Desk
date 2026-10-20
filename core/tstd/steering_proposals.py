"""Agent-proposed steering edits, written only on the user's approval (TD-5140).

The agent never writes a steering file (AGENTS.md directive 4). The
``propose_steering_edit`` tool reads the file, builds a diff, parks it on
the session and emits ``steering_proposal``; it writes nothing. Only the
user's Accept, or Edit then Save, writes the file, and the daemon writes
it here, never through ``fs_write``: the fs tools still refuse every
steering write.

Before writing, the daemon checks the path is still a proposable steering
file inside the workspace (no symlink swapped in since) and that the file
still holds what the diff was built against. A changed file is refused
with a fresh diff in place of the stale one. Every decision goes in the
session log with its diff. Nothing here runs without a user message:
skip-all and autonomy runs never accept a proposal.

What the card shows is what gets written. Every logged event is scrubbed
of secret-shaped strings (TD-4802), so a proposal whose path, file or
text the scrub would change is refused: the card would show blanks,
Accept would write text the user never saw, and Edit would start from
the blanks and save them over the real text. Save refuses such a string
in the user's own text for the same reason (the logged diff must show
what was written).

Parked proposals live in memory on the session. A restart drops them,
and the daemon then logs each one the log still shows open as
``expired`` when it restores the session, so the attach replay does not
bring back a card nothing can act on.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import os
import stat
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .autonomy.charter import CHARTER_RELATIVE_PARTS
from .autonomy.classifier import (
    Boundary,
    canonical_path,
    is_exec_later_write,
    is_steering_write,
    relative_parts,
)
from .logging import get_logger, redact_secrets
from .protocol import (
    DaemonEvent,
    SteeringAccept,
    SteeringDecision,
    SteeringEdit,
    SteeringFileDiff,
    SteeringProposal,
    SteeringReject,
    build_error,
)

if TYPE_CHECKING:
    from .session import Session

log = get_logger("tstd.steering_proposals")

#: Largest proposed text, and largest current file a proposal can replace.
STEERING_TEXT_CAP = 256 * 1024

STALE_MESSAGE = "The file changed since this was proposed. A fresh diff is on the card."

_INSTRUCTION_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "SKILL.md"})
_RULES_PARTS = (".tst", "rules")
# The workspace command trees the loader reads (context/commands.py). It
# reads ``.tstdesk/commands`` only under the home directory, so a proposal
# there inside a workspace would land a file nothing uses (TD-5158).
_COMMAND_PAIRS = frozenset({(".tst", "commands"), (".claude", "commands")})
_POLICY_PARTS = (".tst", "config.yaml")
_PROTECTED = frozenset({_POLICY_PARTS, tuple(p.casefold() for p in CHARTER_RELATIVE_PARTS)})

_NOT_PROPOSABLE = (
    "Only a steering file can be proposed: AGENTS.md, CLAUDE.md, a .tst/rules/*.md rule, "
    "a SKILL.md, or a slash-command file (.tst/commands/*.md). Change other files with "
    "fs_edit or fs_write."
)
_PROTECTED_MESSAGE = (
    "The signed charter (.tst/autonomy/CHARTER.md) and the approval policy "
    "(.tst/config.yaml) can't be proposed. Only the user edits them."
)

#: Refused because the session log's scrub would blank part of the card.
SECRET_SHAPED = "steering_secret_shaped"
_SHAPES = (
    "a long run of letters, digits, '-' or '_' after a token prefix such as sk-, ghp_ or "
    "github_pat_, an AWS key id, or a private-key header; ordinary words such as "
    "task-tool-availability can match too"
)
_HIDDEN_MESSAGES = {
    "path": (
        "The file name has a string shaped like a secret, which the card would blank, so the "
        "user couldn't see which file Accept writes. Pick another name."
    ),
    "before": (
        f"The file already holds a string shaped like a secret ({_SHAPES}). The card would "
        "blank it, so the user couldn't check this edit. Ask the user to change the file by "
        "hand."
    ),
    "after": (
        f"The proposed text has a string shaped like a secret ({_SHAPES}). The card would "
        "blank it, so the user couldn't see what Accept writes. Keep secrets out of steering "
        "files: reword it and propose again."
    ),
}
_HIDDEN_IN_EDIT = (
    f"Your text has a string shaped like a secret ({_SHAPES}). The session log would blank "
    "it, so the logged diff couldn't show what was written. Reword it, or change the file "
    "by hand."
)
_HIDDEN_AFTER_CHANGE = (
    "The file changed since this was proposed, and it now holds a string shaped like a "
    "secret, which the card would blank, so no fresh diff can be shown. Reject this "
    "proposal and change the file by hand."
)
NOT_OPEN_MESSAGE = (
    "That steering proposal is no longer open, and nothing was written. Ask the agent to "
    "propose it again."
)


class SteeringRefusal(Exception):
    """A proposal or a decision was refused, with a typed code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Stale(Exception):
    """The file no longer holds what the diff was built against."""

    def __init__(self, current: str | None) -> None:
        super().__init__("stale")
        self.current = current


@dataclass(frozen=True)
class ParkedSteering:
    """One proposal waiting for the user, with the exact text.

    ``before`` is the file as the diff saw it (``None``: did not exist).
    ``workspace`` is the canonical root it was proposed in. ``user_text``
    says ``after`` is the user's own edit carried onto a fresh card after
    the file changed, so accepting it is logged as ``edited`` (TD-5158).
    """

    proposal_id: str
    workspace: str
    relative: str
    before: str | None
    after: str
    tool_call_id: str = ""
    autonomy: bool = False
    user_text: bool = False

    @property
    def action(self) -> Literal["create", "replace"]:
        return "create" if self.before is None else "replace"


# ── Which paths can be proposed ────────────────────────────────────────


def proposable_relative(workspace_root: Path, target: Path) -> str:
    """The workspace-relative posix path of *target*, if it may be proposed.

    *target* is canonicalized first (symlinks resolved), so a link that
    leads out of the workspace or onto a non-steering file is refused.
    Names are matched exactly: the assembler reads ``AGENTS.md`` and
    ``.tst/rules/*.md``, and a case variant would land a file nothing
    reads.
    """
    root = canonical_path(workspace_root)
    canonical = canonical_path(target)
    boundary = Boundary(workspace_root=root)
    parts = relative_parts(canonical, root)
    if not parts:
        raise SteeringRefusal("outside_workspace", f"path outside the workspace: {canonical}")
    if tuple(p.casefold() for p in parts) in _PROTECTED:
        raise SteeringRefusal("steering_protected", _PROTECTED_MESSAGE)
    if is_exec_later_write(boundary, canonical) or not is_steering_write(boundary, canonical):
        raise SteeringRefusal("not_steering", _NOT_PROPOSABLE)
    if not _is_proposable_kind(parts):
        raise SteeringRefusal("not_steering", _NOT_PROPOSABLE)
    return "/".join(parts)


def _is_proposable_kind(parts: list[str]) -> bool:
    name = parts[-1]
    if name in _INSTRUCTION_NAMES:
        return True
    if not name.endswith(".md") or len(parts) != 3:
        return False
    head = (parts[0], parts[1])
    return head == _RULES_PARTS or head in _COMMAND_PAIRS


# ── Reading, diffing, writing ──────────────────────────────────────────


def read_current(path: Path) -> str | None:
    """The file's text, or ``None`` when it does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise SteeringRefusal("not_a_file", f"{path.name} is not a regular file")
    if info.st_size > STEERING_TEXT_CAP:
        raise SteeringRefusal("too_large", f"{path.name} is over {STEERING_TEXT_CAP} bytes")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SteeringRefusal("not_text", f"{path.name} is not UTF-8 text") from None


def unified_diff(before: str | None, after: str, relative: str) -> str:
    """A unified diff in the memory card's shape (``a/`` and ``b/`` labels)."""
    return "".join(
        difflib.unified_diff(
            _lines(before or ""),
            _lines(after),
            fromfile="/dev/null" if before is None else f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )


def _lines(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = f"{lines[-1]}\n"
    return lines


def file_diff(parked: ParkedSteering, after: str | None = None) -> SteeringFileDiff:
    """The wire shape of *parked*, optionally with other written text."""
    text = parked.after if after is None else after
    return SteeringFileDiff(
        action=parked.action,
        path=parked.relative,
        diff=unified_diff(parked.before, text, parked.relative),
        before=parked.before,
        after=text,
    )


def hidden_part(diff: SteeringFileDiff) -> Literal["path", "before", "after"] | None:
    """Which part of *diff* the session log's secret scrub would change, if any.

    The card is drawn from the logged event, so a part the scrub changes
    is a part the user can't see as it is. ``after`` also covers the diff
    text itself (its lines come from ``before`` and ``after``).
    """
    if redact_secrets(diff.path) != diff.path:
        return "path"
    if diff.before is not None and redact_secrets(diff.before) != diff.before:
        return "before"
    if diff.after is not None and redact_secrets(diff.after) != diff.after:
        return "after"
    if redact_secrets(diff.diff) != diff.diff:
        return "after"
    return None


def write_steering_file(root: Path, relative: str, text: str) -> None:
    """Write *text* to *relative* under *root* atomically: a temp file, then rename.

    The rename replaces the directory entry, so a hardlinked file is
    never written through. An existing file keeps its permission bits.

    Where the OS allows it, every directory under *root* is opened with
    ``O_NOFOLLOW`` and the file is written relative to the last one, so
    the path is resolved once. The agent's turn keeps running while the
    card is open, and a shell command could swap a directory on the
    checked path for a symlink between the check and the write; the user's
    text would then land wherever the link points (TD-5158). A swapped
    directory now fails the write instead.
    """
    if not _DIR_FD_WRITES:
        _write_by_path(root.joinpath(*relative.split("/")), text)
        return
    *dirs, name = relative.split("/")
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in dirs:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, dir_fd=dir_fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child
        _write_in_dir(dir_fd, name, text)
    finally:
        os.close(dir_fd)


#: ``openat``-style writes: POSIX. Windows takes the path-based write; a
#: symlink there needs a privilege an agent's shell does not have.
_DIR_FD_WRITES = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)


def _write_in_dir(dir_fd: int, name: str, text: str) -> None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode) if stat.S_ISREG(info.st_mode) else 0o644
    except FileNotFoundError:
        mode = 0o644
    tmp = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _write_by_path(target: Path, text: str) -> None:
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ── The tool side ──────────────────────────────────────────────────────


def proposal_event(
    session_id: str, parked: ParkedSteering, replaces: str | None = None
) -> SteeringProposal:
    return SteeringProposal(
        session_id=session_id,
        proposal_id=parked.proposal_id,
        files=[file_diff(parked)],
        tool_call_id=parked.tool_call_id,
        replaces=replaces,
        autonomy=parked.autonomy,
        seq=1,
    )


async def propose(session: Session, path: str, text: str, tool_call_id: str = "") -> ParkedSteering:
    """Park a proposal on *session* and log ``steering_proposal``. Writes nothing."""
    root = Path(session.workspace_path)
    raw = Path(path)
    relative = proposable_relative(root, raw if raw.is_absolute() else root / raw)
    if text == "":
        raise SteeringRefusal(
            "empty_text", "Propose the whole new file. Deleting a steering file is the user's job."
        )
    if len(text) > STEERING_TEXT_CAP:
        raise SteeringRefusal("too_large", f"The proposed text is over {STEERING_TEXT_CAP} chars")
    before = await asyncio.to_thread(read_current, canonical_path(root / relative))
    if before == text:
        raise SteeringRefusal("no_change", f"{relative} already has exactly this text.")
    parked = ParkedSteering(
        proposal_id=f"sp-{uuid.uuid4()}",
        workspace=str(canonical_path(root)),
        relative=relative,
        before=before,
        after=text,
        tool_call_id=tool_call_id,
        autonomy=bool(getattr(session, "autonomy", False)),
    )
    hidden = hidden_part(file_diff(parked))
    if hidden is not None:
        raise SteeringRefusal(SECRET_SHAPED, _HIDDEN_MESSAGES[hidden])
    session.steering_proposals[parked.proposal_id] = parked
    await session.event_log.add(proposal_event(session.id, parked))
    log.info(
        "steering edit proposed",
        extra={"extra_fields": {"session_id": session.id, "path": relative}},
    )
    return parked


def pending_paths(session: Session) -> list[str]:
    """Open proposals as ``path (action)`` lines, for the run summary."""
    return [f"{p.relative} ({p.action})" for p in session.steering_proposals.values()]


# ── The user side ──────────────────────────────────────────────────────


async def decide(
    session: Session, msg: SteeringAccept | SteeringEdit | SteeringReject
) -> str | None:
    """Apply the user's decision. ``None`` on success, else an error frame.

    Reached only from a client message: no other code path accepts. The
    proposal is taken off the session first, so a second click on the
    same card finds nothing to write; a refused write puts it back.
    """
    parked = session.steering_proposals.pop(msg.proposal_id, None)
    if parked is None:
        return _error(session, msg, "no_steering_proposal", NOT_OPEN_MESSAGE)
    if isinstance(msg, SteeringReject):
        await _log_decision(session, parked, "rejected", file_diff(parked))
        return None
    text = parked.after if isinstance(msg, SteeringAccept) else msg.content
    try:
        if text == "":
            raise SteeringRefusal(
                "steering_edit_empty",
                "An empty file can't be saved from the card. Reject the proposal instead.",
            )
        if len(text) > STEERING_TEXT_CAP:
            raise SteeringRefusal("too_large", f"The text is over {STEERING_TEXT_CAP} chars")
        if hidden_part(file_diff(parked, after=text)) is not None:
            # Accept: never, since propose() refused it. Save: the user's own
            # text, which the logged decision could not show as written.
            raise SteeringRefusal(SECRET_SHAPED, _HIDDEN_IN_EDIT)
        await asyncio.to_thread(_write_if_unchanged, session, parked, text)
    except _Stale as stale:
        return await _offer_fresh(session, parked, text, stale.current, msg)
    except SteeringRefusal as refusal:
        session.steering_proposals[parked.proposal_id] = parked
        return _error(session, msg, refusal.code, refusal.message)
    except OSError as exc:
        session.steering_proposals[parked.proposal_id] = parked
        reason = exc.strerror or type(exc).__name__
        return _error(
            session, msg, "steering_write_failed", f"Couldn't write {parked.relative}: {reason}"
        )
    edited = parked.user_text or text != parked.after
    decision: Literal["accepted", "edited"] = "edited" if edited else "accepted"
    await _log_decision(session, parked, decision, file_diff(parked, after=text))
    return None


def _error(
    session: Session, msg: SteeringAccept | SteeringEdit | SteeringReject, code: str, text: str
) -> str:
    return build_error(
        code, text, session_id=session.id, in_reply_to=msg.type, proposal_id=msg.proposal_id
    )


def _write_if_unchanged(session: Session, parked: ParkedSteering, text: str) -> None:
    """Re-check the path and the file, then write. Runs off the event loop."""
    root = Path(session.workspace_path)
    if str(canonical_path(root)) != parked.workspace:
        # A live move (TD-1715) since: the diff was of a file in the old root.
        raise SteeringRefusal(
            "steering_workspace_moved",
            "This session moved to another folder since this was proposed. Reject it and "
            "ask the agent again.",
        )
    target = canonical_path(root / parked.relative)
    if proposable_relative(root, target) != parked.relative:
        raise SteeringRefusal("not_steering", _NOT_PROPOSABLE)
    current = read_current(target)
    if current != parked.before:
        raise _Stale(current)
    write_steering_file(canonical_path(root), parked.relative, text)


async def _offer_fresh(
    session: Session,
    parked: ParkedSteering,
    text: str,
    current: str | None,
    msg: SteeringAccept | SteeringEdit,
) -> str:
    """Swap a stale proposal for a fresh diff against the file as it is now.

    The fresh one carries the text the user was saving, so an edit made on
    the stale card is not lost.
    """
    fresh = replace(
        parked,
        proposal_id=f"sp-{uuid.uuid4()}",
        before=current,
        after=text,
        user_text=parked.user_text or text != parked.after,
    )
    if hidden_part(file_diff(fresh)) is not None:
        # The card could not show the file as it is now. Keep the stale one
        # so Reject still logs a decision; Accept stays refused as stale.
        session.steering_proposals[parked.proposal_id] = parked
        return _error(session, msg, SECRET_SHAPED, _HIDDEN_AFTER_CHANGE)
    session.steering_proposals[fresh.proposal_id] = fresh
    await session.event_log.add(proposal_event(session.id, fresh, replaces=parked.proposal_id))
    return _error(session, msg, "steering_proposal_stale", STALE_MESSAGE)


async def _log_decision(
    session: Session,
    parked: ParkedSteering,
    decision: Literal["accepted", "edited", "rejected"],
    diff: SteeringFileDiff,
) -> None:
    event = SteeringDecision(
        session_id=session.id,
        proposal_id=parked.proposal_id,
        decision=decision,
        files=[diff],
        seq=1,
    )
    await session.event_log.add(event)
    fields = {"session_id": session.id, "path": parked.relative, "decision": decision}
    log.info("steering proposal decided", extra={"extra_fields": fields})


# ── After a restart ────────────────────────────────────────────────────


def open_in_log(events: Sequence[DaemonEvent]) -> list[SteeringProposal]:
    """Proposals the log shows open: no decision, and no fresh diff replaced them."""
    closed: set[str] = set()
    for event in events:
        if isinstance(event, SteeringDecision):
            closed.add(event.proposal_id)
        elif isinstance(event, SteeringProposal) and event.replaces:
            closed.add(event.replaces)
    found: dict[str, SteeringProposal] = {}
    for event in events:
        if isinstance(event, SteeringProposal) and event.proposal_id not in closed:
            found.setdefault(event.proposal_id, event)
    return list(found.values())


async def expire_restored(session: Session, events: Sequence[DaemonEvent]) -> int:
    """Log ``expired`` for every proposal a restart dropped. Returns how many.

    Parked proposals live in memory, so a restored session holds none. Left
    open in the log, the attach replay would bring their cards back with
    Accept live, and every click would find nothing to write. The decision
    carries the logged diff and writes nothing. Once logged, a second
    restore finds nothing to close.
    """
    expired = [p for p in open_in_log(events) if p.proposal_id not in session.steering_proposals]
    for proposal in expired:
        await session.event_log.add(
            SteeringDecision(
                session_id=session.id,
                proposal_id=proposal.proposal_id,
                decision="expired",
                files=proposal.files,
                seq=1,
            )
        )
    if expired:
        log.info(
            "steering proposals closed by a restart",
            extra={"extra_fields": {"session_id": session.id, "count": len(expired)}},
        )
    return len(expired)
