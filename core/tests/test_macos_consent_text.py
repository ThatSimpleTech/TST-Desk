"""The macOS Screen Recording prompt says where screenshots go (HOST-11).

The OS shows this sentence at the one consent moment for screen capture. It
said "Nothing is recorded or sent off this Mac", but each computer-use
screenshot goes into the next request to the chat's model, often a cloud API.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parents[2] / "shell" / "macos-info.plist"


def test_the_prompt_says_screenshots_go_to_the_model() -> None:
    text = plistlib.loads(PLIST.read_bytes())["NSScreenCaptureUsageDescription"]
    assert "sent to the model" in text
    assert "sent off this Mac" not in text
