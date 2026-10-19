"""Queued user messages outlive a restart (TD-5155).

A message the daemon accepted is the daemon's to keep (TD-5019). The
loop's queue lives in memory, so every message waiting for it is also in
``sessions/<id>/queued.json`` before the send returns, and leaves once its
``user_turn`` and the conversation are saved. The loop logs that turn under
the message's own id, so a stop between the two is settled on restore: a
message whose turn is in the log already ran.

What a restart finds is brought back *held*: logged as ``queued_messages``
and sent only when the person picks Send (AGENTS §2.3, TD-5018). Image
bytes are never written here. A held message keeps its text, counts the
images it lost, and tells the model they were dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .logging import get_logger
from .protocol import DaemonEvent, QueuedMessages, UserTurn
from .session import QueuedUserMessage, Session

log = get_logger("tstd.queued_messages")


def dropped_note(count: int) -> str:
    """What the model reads in place of images a restart did not keep."""
    noun = "image" if count == 1 else "images"
    verb = "was" if count == 1 else "were"
    return f"[{count} attached {noun} did not survive a TST Desk restart and {verb} not sent.]"


def _flatten(provider: str | list[dict[str, Any]]) -> tuple[str, int]:
    """Provider content as text, and how many image parts it had."""
    if isinstance(provider, str):
        return provider, 0
    texts = [str(p.get("text", "")) for p in provider if p.get("type") == "text"]
    images = sum(1 for p in provider if p.get("type") != "text")
    text = "\n\n".join(t for t in texts if t)
    if images:
        note = dropped_note(images)
        text = f"{text}\n\n{note}" if text else note
    return text, images


def queue_rows(messages: Sequence[QueuedUserMessage]) -> list[dict[str, Any]]:
    """The queue file's rows: text only, with dropped images counted."""
    rows: list[dict[str, Any]] = []
    for message in messages:
        text, images = _flatten(message.provider)
        rows.append(
            {
                "id": message.id,
                "display": message.display,
                "provider": text,
                "images_dropped": message.images_dropped + images,
            }
        )
    return rows


def messages_from_rows(rows: Sequence[dict[str, Any]]) -> list[QueuedUserMessage]:
    """Rows back as messages. A row missing a field is skipped and logged."""
    messages: list[QueuedUserMessage] = []
    for row in rows:
        ident, display, provider = row.get("id"), row.get("display"), row.get("provider")
        dropped = row.get("images_dropped", 0)
        if not (
            isinstance(ident, str)
            and ident
            and isinstance(display, str)
            and isinstance(provider, str)
            and isinstance(dropped, int)
            and dropped >= 0
        ):
            log.warning("dropping malformed queued message")
            continue
        messages.append(
            QueuedUserMessage(display=display, provider=provider, id=ident, images_dropped=dropped)
        )
    return messages


async def restore_queue(
    sess: Session, rows: Sequence[dict[str, Any]], events: Sequence[DaemonEvent]
) -> None:
    """Hold what the last daemon left queued, and log the list (TD-5155).

    Runs after the persist hooks are attached. A message whose
    ``user_turn`` is in *events* already started, so it is not held again.
    The list is logged when anything is held, and also when the log's
    last list named messages that are gone, so a replay never shows a row
    the daemon no longer has.
    """
    started = {e.turn_id for e in events if isinstance(e, UserTurn)}
    held = [m for m in messages_from_rows(rows) if m.id not in started]
    if rows:
        await sess.restore_held(held)
    last = next((e for e in reversed(events) if isinstance(e, QueuedMessages)), None)
    if held or (last is not None and last.messages):
        await sess.log_held_messages()
    if held:
        log.info(
            "held queued messages after restart",
            extra={"extra_fields": {"session_id": sess.id, "held": len(held)}},
        )


async def keep_held_visible(sess: Session) -> None:
    """Log the held list again when the log window no longer has it.

    The event log keeps the newest events only, so a long session can
    trim the last ``queued_messages`` away while messages are still held.
    A window attaching then would not see them.
    """
    if not sess.held_messages:
        return
    if any(isinstance(e, QueuedMessages) for e in sess.event_log.all_events):
        return
    await sess.log_held_messages()
