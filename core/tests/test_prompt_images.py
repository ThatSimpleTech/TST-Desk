"""Keep at most N images on the outbound prompt (TD-1730)."""

from __future__ import annotations

from tstd.prompt_images import cap_prompt_images
from tstd.provider import ChatMessage


def _shot(n: int) -> ChatMessage:
    return ChatMessage(
        role="tool",
        tool_call_id=f"c{n}",
        content=[
            {"type": "text", "text": f'{{"width": {n}}}'},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,AAA{n}"},
            },
        ],
    )


def test_keeps_only_the_newest_image() -> None:
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="go"),
        _shot(1),
        _shot(2),
    ]
    dropped = cap_prompt_images(messages, 1)
    assert dropped == 1
    assert messages[2].content == '{"width": 1}'
    assert isinstance(messages[3].content, list)
    assert messages[3].content[1]["type"] == "image_url"


def test_zero_strips_all_images() -> None:
    messages = [_shot(1), _shot(2)]
    assert cap_prompt_images(messages, 0) == 2
    assert messages[0].content == '{"width": 1}'
    assert messages[1].content == '{"width": 2}'


def test_negative_is_unlimited() -> None:
    messages = [_shot(1), _shot(2)]
    assert cap_prompt_images(messages, -1) == 0
    assert isinstance(messages[0].content, list)
    assert isinstance(messages[1].content, list)
