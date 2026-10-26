"""Cut a reply that has turned to junk, and ask again (TD-5175).

A small or heavily quantized local model (the 1-bit DeepSeek forge) can
lose its footing partway through a long reply: random characters from
scripts nobody in the conversation writes, one short pattern repeated
for thousands of tokens, tool-call markup leaking into the text, or
thinking that runs to the output cap. The provider reports each of these
as a normal finish, so the turn went on as if it had an answer, and the
server kept generating junk for minutes.

With ``reply_guard`` set on a tier, the stream is watched as it arrives.
The first sign ends the call at once (the hang-up also stops the
server), the partial reply is withdrawn the way a stall is (TD-5159), and
the same request is sent again, at most ``retries`` times. After a
thinking cap trip the retry merges ``retry_extra_body`` into the request,
so a forge tier can answer that step with thinking off.

All checks look only at text the model wrote in this call. A script the
user writes in (Spanish, Russian, Arabic) never counts as junk.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import ReplyGuardConfig

#: Characters of recent output the junk and repetition checks look at.
WINDOW = 800
#: The junk check needs at least this much text before it judges.
MIN_JUNK_TEXT = 300
#: Run the pattern checks once per this many new characters.
CHECK_EVERY = 200

LEAK_MARKERS = (
    "\uff5cDSML\uff5c",  # DeepSeek DSML tag (fullwidth bars)
    "<\uff5c",
    "</think>",
    "<think>",
    "<tool_call>",
    "</tool_call>",
    "<|tool_call",
)

_SEPARATOR_CHARS = set("-=_~*#|─━ \t\r\n.")
# Short unit repeated many times (``448448…``, ``**>**>…``).
_SHORT_REPEAT = re.compile(r"(.{1,4}?)\1{39,}", re.S)
# Longer unit repeated back to back (a DSML closing tag x 40).
_LONG_REPEAT = re.compile(r"(.{5,60}?)\1{7,}", re.S)


def _is_scripted(ch: str) -> bool:
    """A letter from a script with its own block (Greek, Cyrillic, Arabic, Indic, Thai, Hangul).

    Latin (Spanish included), punctuation, arrows, box drawing, CJK,
    fullwidth forms and emoji are not counted: code and markdown use them,
    and the DSML tool markup is written in fullwidth bars.
    """
    o = ord(ch)
    return not (
        o < 0x250
        or 0x2000 <= o <= 0x2BFF
        or 0x3000 <= o <= 0x9FFF
        or 0xFF00 <= o <= 0xFFEF
        or o >= 0x1F000
    )


def _block(ch: str) -> int:
    return ord(ch) >> 7


def user_blocks(texts: Iterable[str]) -> frozenset[int]:
    """Unicode blocks the user's own messages use: never junk in a reply."""
    return frozenset(_block(c) for t in texts for c in t if _is_scripted(c))


class ReplyGuard:
    """Watches one model call's stream; :meth:`feed` returns why to cut it, or None."""

    def __init__(
        self,
        config: ReplyGuardConfig,
        *,
        allowed_blocks: frozenset[int] = frozenset(),
        tools: bool = True,
    ):
        self._cfg = config
        self._allowed = allowed_blocks
        self._tools = tools
        self._recent = ""
        self._reasoning_chars = 0
        self._content = ""
        self._since_check = 0

    def feed(self, *, reasoning: str = "", content: str = "") -> str | None:
        if reasoning:
            self._reasoning_chars += len(reasoning)
            cap = self._cfg.max_reasoning_chars
            if cap and self._reasoning_chars > cap:
                return "thinking_cap"
        if content:
            self._content += content
            if self._cfg.leaked_markup and self._tools:
                tail = self._content[-(len(content) + 16) :]
                if any(m in tail for m in LEAK_MARKERS):
                    return "leaked_markup"
        new = reasoning + content
        if not new:
            return None
        self._recent = (self._recent + new)[-WINDOW:]
        self._since_check += len(new)
        if self._since_check < CHECK_EVERY:
            return None
        self._since_check = 0
        return self._check_window()

    def _check_window(self) -> str | None:
        text = self._recent
        if self._cfg.junk and len(text) >= MIN_JUNK_TEXT:
            junk = sum(1 for c in text if _is_scripted(c) and _block(c) not in self._allowed)
            if junk >= self._cfg.junk_ratio * len(text):
                return "junk"
        if self._cfg.repetition:
            for pattern in (_SHORT_REPEAT, _LONG_REPEAT):
                m = pattern.search(text)
                if m and not set(m.group(1)) <= _SEPARATOR_CHARS:
                    return "repetition"
        return None
