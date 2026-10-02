"""Inline Markdown for an email report (TD-3821).

Emphasis, code, and links. Images are not emitted. A link that is not
http or https is the label only, so ``javascript:`` never becomes an
href. Text is escaped before it is wrapped in a tag.
"""

from __future__ import annotations

import html

from .email_markdown import FONT, PALETTE, _code_style, css

_ESCAPABLE = set("\\`*_{}[]()#+-.!")
_STOP = set("\\`*_![")
_MAX_DEPTH = 6


def render_inline(text: str, depth: int = 0) -> str:
    """*text* as HTML. No block structure."""
    if not text:
        return ""
    if depth > _MAX_DEPTH:
        return html.escape(text)
    parts: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length and text[index + 1] in _ESCAPABLE:
            parts.append(html.escape(text[index + 1]))
            index += 2
            continue
        if char == "`":
            end = text.find("`", index + 1)
            if end > index + 1:
                escaped = html.escape(text[index + 1 : end])
                parts.append(f'<code style="{_code_style()}">{escaped}</code>')
                index = end + 1
                continue
        if text.startswith(("**", "__"), index):
            marker = text[index : index + 2]
            end = text.find(marker, index + 2)
            if end > index + 2:
                inner = render_inline(text[index + 2 : end], depth + 1)
                parts.append(f'<strong style="{_emph_style()}">{inner}</strong>')
                index = end + 2
                continue
        if char in "*_":
            close = _emphasis_end(text, index, char)
            if close is not None:
                inner = render_inline(text[index + 1 : close], depth + 1)
                parts.append(f'<em style="{_emph_style()}">{inner}</em>')
                index = close + 1
                continue
        if char == "!" and index + 1 < length and text[index + 1] == "[":
            parsed = _parse_link(text, index + 1)
            if parsed is not None:
                label, _dest, nxt = parsed
                # No images, https or otherwise. The alt text stays.
                parts.append(render_inline(label, depth + 1))
                index = nxt
                continue
        if char == "[":
            parsed = _parse_link(text, index)
            if parsed is not None:
                label, dest, nxt = parsed
                parts.append(_anchor(label, dest, depth))
                index = nxt
                continue
        nxt = index + 1
        while nxt < length and text[nxt] not in _STOP:
            nxt += 1
        parts.append(html.escape(text[index:nxt]))
        index = nxt
    return "".join(parts)


def _emph_style() -> str:
    return css(("font-family", FONT), ("color", PALETTE["ink"]))


def _emphasis_end(text: str, start: int, char: str) -> int | None:
    """Closing index for an opener at *start*, or None.

    ``file_name`` is not emphasis. A marker glued to another marker is
    the bold delimiter, not this closer.
    """
    if start > 0 and (text[start - 1].isalnum() or text[start - 1] == char):
        return None
    if start + 1 >= len(text) or text[start + 1].isspace() or text[start + 1] == char:
        return None
    cursor = start + 1
    while cursor < len(text):
        if text[cursor] != char:
            cursor += 1
            continue
        prev = text[cursor - 1]
        nxt = text[cursor + 1] if cursor + 1 < len(text) else ""
        if prev == char or nxt == char or prev.isspace() or nxt.isalnum():
            cursor += 1
            continue
        return cursor
    return None


def _parse_link(text: str, index: int) -> tuple[str, str, int] | None:
    close = text.find("](", index + 1)
    if close == -1:
        return None
    end = _matching_paren(text, close + 1)
    if end is None:
        return None
    return text[index + 1 : close], _destination(text[close + 2 : end]), end + 1


def _matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for cursor in range(open_index, len(text)):
        if text[cursor] == "(":
            depth += 1
        elif text[cursor] == ")":
            depth -= 1
            if depth == 0:
                return cursor
    return None


def _destination(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if text.startswith("<"):
        end = text.find(">")
        if end != -1:
            return text[1:end].strip()
    if " " in text:
        return text.split(None, 1)[0]
    return text


def _anchor(label: str, dest: str, depth: int) -> str:
    inner = render_inline(label, depth + 1)
    if not _http_url(dest):
        return inner
    href = html.escape(dest, quote=True)
    style = css(
        ("font-family", FONT),
        ("color", PALETTE["link"]),
        ("text-decoration", "underline"),
    )
    return f'<a href="{href}" style="{style}">{inner}</a>'


def _http_url(url: str) -> bool:
    """True for one http(s) URL. ``javascript:`` and ``data:`` are not."""
    if not url or len(url) > 2000:
        return False
    if any(char.isspace() or ord(char) < 32 or char in "<>\"'`" for char in url):
        return False
    lowered = url.lower()
    if lowered.startswith("https://"):
        rest = url[8:]
    elif lowered.startswith("http://"):
        rest = url[7:]
    else:
        return False
    return bool(rest) and not rest.startswith(("/", "\\"))
