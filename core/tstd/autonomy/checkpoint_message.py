"""Checkpoint commit messages: written by the checkpointer, read by the rewind (TD-5125).

Every commit on ``tst/session/<id>`` says which session made it, what kind
it is, and where it falls in that session's event log:

- ``Kind: agent`` is an agent write (TD-705).
- ``Kind: preimage`` is the file as it stood on disk just before an agent
  write, recorded only when that differed from the branch: the person's own
  changes, made before or between the agent's writes.
- ``Kind: restore`` is "Restore files to here" putting files back.

``Log-Seq`` is the session log's ``last_seq`` when the commit was made, so a
user turn (its ``user_turn`` event's seq) splits the branch into before and
after. A commit made before TD-5125 has no ``Log-Seq``.

Every value is escaped onto one line (:func:`one_line`), so a file name with
a newline cannot write a trailer of its own. The reader takes the first line
that starts with each key; the subject never starts with one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

KIND_AGENT = "agent"
KIND_PREIMAGE = "preimage"
KIND_RESTORE = "restore"
_KINDS = frozenset({KIND_AGENT, KIND_PREIMAGE, KIND_RESTORE})


@dataclass(frozen=True)
class Trailers:
    """What the rewind reads off one checkpoint commit."""

    session_id: str
    kind: str
    log_seq: int | None


def one_line(text: str) -> str:
    """*text* with every control character escaped, so it stays on its line."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _body(lines: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"{key}: {one_line(value)}" for key, value in lines)


def agent_message(
    *,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    decision: str,
    via: str,
    rels: Sequence[str],
    log_seq: int | None,
) -> str:
    """An agent write's message (TD-705 lines, plus ``Kind`` and ``Log-Seq``)."""
    subject = f"checkpoint({decision}): {one_line(tool_name)} {one_line(rels[0])}"
    lines = [
        ("Session", session_id),
        ("Tool-Call", tool_call_id),
        ("Decision", f"class {decision} via {via}"),
        ("Kind", KIND_AGENT),
    ]
    if log_seq is not None:
        lines.append(("Log-Seq", str(log_seq)))
    lines.append(("Paths", ", ".join(rels)))
    return f"{subject}\n\n{_body(lines)}"


def preimage_message(
    *, session_id: str, tool_name: str, tool_call_id: str, rels: Sequence[str], log_seq: int | None
) -> str:
    """The files as they were on disk before an agent write."""
    subject = f"checkpoint(user): before {one_line(tool_name)} {one_line(rels[0])}"
    lines = [("Session", session_id), ("Tool-Call", tool_call_id), ("Kind", KIND_PREIMAGE)]
    if log_seq is not None:
        lines.append(("Log-Seq", str(log_seq)))
    lines.append(("Paths", ", ".join(rels)))
    return f"{subject}\n\n{_body(lines)}"


def restore_message(
    *, session_id: str, turn_id: str, rels: Sequence[str], log_seq: int | None
) -> str:
    """Files "Restore files to here" put back."""
    subject = f"restore: {len(rels)} file(s) to before turn {one_line(turn_id)}"
    lines = [("Session", session_id), ("Turn", turn_id), ("Kind", KIND_RESTORE)]
    if log_seq is not None:
        lines.append(("Log-Seq", str(log_seq)))
    lines.append(("Paths", ", ".join(rels)))
    return f"{subject}\n\n{_body(lines)}"


def parse_trailers(message: str) -> Trailers | None:
    """The trailers of a checkpoint message, or None for any other commit.

    A commit from before TD-5125 has no ``Kind``: it was an agent write.
    """
    found: dict[str, str] = {}
    for line in message.splitlines()[1:]:
        key, sep, value = line.partition(": ")
        if sep and key in ("Session", "Kind", "Log-Seq") and key not in found:
            found[key] = value
    session_id = found.get("Session")
    if not session_id:
        return None
    kind = found.get("Kind", KIND_AGENT)
    if kind not in _KINDS:
        return None
    raw_seq = found.get("Log-Seq")
    log_seq = int(raw_seq) if raw_seq is not None and raw_seq.isdigit() else None
    return Trailers(session_id=session_id, kind=kind, log_seq=log_seq)
