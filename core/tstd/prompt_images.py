"""Cap vision parts on the outbound prompt (TD-1730).

A local VLM is often served with ``--limit-mm-per-prompt '{"image":1}'``. A second
``desktop_screenshot`` would otherwise send two ``image_url`` parts and
the host rejects the turn. Keep the newest images; drop older ones in
place. Text of those tool results stays, and a note takes each dropped
image's place: the text beside it may say an image follows (``fs_read``,
TD-5130), and a model told so with nothing there describes what it never
saw.
"""

from __future__ import annotations

from .provider import ChatMessage


def dropped_image_note(max_images: int) -> str:
    """What stands where an image was dropped from the prompt."""
    kept = "no images" if max_images == 0 else f"only the newest {max_images}"
    return (
        f"[Image not shown: this model is sent {kept} per request. "
        "Read or attach it again to see it.]"
    )


def cap_prompt_images(messages: list[ChatMessage], max_images: int) -> int:
    """Drop oldest ``image_url`` parts so at most *max_images* remain.

    Returns how many image parts were removed. ``max_images < 0`` is a
    no-op (unlimited). ``0`` strips every image.
    """
    if max_images < 0:
        return 0
    slots: list[tuple[int, int]] = []
    for index, message in enumerate(messages):
        content = message.content
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                slots.append((index, part_index))
    drop = slots if max_images == 0 else slots[:-max_images]
    if not drop:
        return 0
    by_message: dict[int, list[int]] = {}
    for message_index, part_index in drop:
        by_message.setdefault(message_index, []).append(part_index)
    note = {"type": "text", "text": dropped_image_note(max_images)}
    for message_index, part_indexes in by_message.items():
        content = messages[message_index].content
        if not isinstance(content, list):
            continue
        dropped = set(part_indexes)
        keep = [dict(note) if i in dropped else part for i, part in enumerate(content)]
        if all(isinstance(part, dict) and part.get("type") == "text" for part in keep):
            texts = [part.get("text") for part in keep]
            messages[message_index].content = "\n".join(t for t in texts if isinstance(t, str))
        else:
            messages[message_index].content = keep
    return len(drop)
