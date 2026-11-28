"""Show hidden and reordering characters as visible markers (RENDERSEC-02).

A summary such as "Run `rm -rf <RLO>/emoh`" carries a right-to-left override,
so it displays as something other than what runs. Every place a person
reads an approval (the card, the OS notification, a phone, a chat
notifier) gets the summary with each such character replaced by a named
marker. The arguments themselves are never changed: they are what runs.
The UI has the same table in ``ui/src/lib/reveal.ts``.
"""

from __future__ import annotations

import re

_HIDDEN = re.compile(
    "[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u061c\u115f\u1160\u180e"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\u3164\ufeff\ufff9-\ufffb]"
)

_NAMES = {
    0x1B: "ESC",
    0x7F: "DEL",
    0x200B: "ZWSP",
    0x200C: "ZWNJ",
    0x200D: "ZWJ",
    0x200E: "LRM",
    0x200F: "RLM",
    0x202A: "LRE",
    0x202B: "RLE",
    0x202C: "PDF",
    0x202D: "LRO",
    0x202E: "RLO",
    0x2066: "LRI",
    0x2067: "RLI",
    0x2068: "FSI",
    0x2069: "PDI",
    0x061C: "ALM",
    0xFEFF: "BOM",
}


def _marker(match: re.Match[str]) -> str:
    code = ord(match.group(0))
    name = _NAMES.get(code)
    return f"⟨{name} U+{code:04X}⟩" if name else f"⟨U+{code:04X}⟩"


def reveal(text: str) -> str:
    """``text`` with every hidden or reordering character shown as a marker."""
    return _HIDDEN.sub(_marker, text)
