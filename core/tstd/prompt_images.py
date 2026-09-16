"""Cap vision parts on the outbound prompt (TD-1730).

ezer-forge is served with ``--limit-mm-per-prompt '{"image":1}'``. A second
``desktop_screenshot`` would otherwise send two ``image_url`` parts and
the host rejects the turn. Keep the newest images; drop older ones in
place. Text of those tool results stays.
"""

from __future__ import annotations

from .provider import ChatMessage


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
    for message_index, part_indexes in by_message.items():
        content = messages[message_index].content
        if not isinstance(content, list):
            continue
        keep = [part for i, part in enumerate(content) if i not in set(part_indexes)]
        if len(keep) == 1 and isinstance(keep[0], dict) and keep[0].get("type") == "text":
            text = keep[0].get("text")
            messages[message_index].content = text if isinstance(text, str) else ""
        else:
            messages[message_index].content = keep if keep else ""
    return len(drop)
