"""Untrusted Markdown to email-safe HTML (TD-3821).

The report is model output. This parser never passes a source tag
through: text is escaped, links are http or https only, and images are
dropped. Colours live in ``PALETTE`` so the shell and the body cannot
drift onto their own hex values. Gmail strips a ``<style>`` block, so
every rule is inline.
"""

from __future__ import annotations

import html
import re
from typing import Final

# Mid blue, not navy. A navy link clears the white card and then fails
# once Gmail's dark theme inverts that card; this value stays near 4:1
# on both. Underline carries the link when a client recolours it anyway.
PALETTE: Final[dict[str, str]] = {
    "page": "#f4f1ea",
    "card": "#ffffff",
    "ink": "#1c1917",
    "muted": "#57534e",
    "band": "#1c1917",
    "band_ink": "#fafaf9",
    "band_muted": "#d6d3d1",
    "line": "#e6e1da",
    "zebra": "#f6f4f1",
    "header_cell": "#efece7",
    "code_bg": "#f6f4f1",
    "quote": "#a8a29e",
    "link": "#4a7bd4",
}

FONT: Final[str] = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO: Final[str] = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\d+)\.\s+(.*)$")
_SEP_CELL = re.compile(r":?-{3,}:?")
_MAX_DEPTH = 6
_HEADING_SIZE = ("22px", "18px", "16px", "15px", "15px", "15px")


def css(*rules: tuple[str, str]) -> str:
    """One inline style string. Callers pass palette values, not new hex."""
    return ";".join(f"{name}:{value}" for name, value in rules)


def bg(color: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Both forms. Some clients read only one of them."""
    return (("background-color", color), ("background", color))


def font(size: str, color: str, line: str) -> str:
    return css(
        ("font-family", FONT),
        ("font-size", size),
        ("line-height", line),
        ("color", color),
    )


def render_markdown(source: str, *, depth: int = 0) -> str:
    """Blocks of *source* as HTML. Empty input is an empty string."""
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    if depth > _MAX_DEPTH:
        # A quote of quotes is not a digest. Stop parsing and escape.
        return _escaped_paragraphs(text)
    lines = text.split("\n")
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        block, index = _block(lines, index, depth)
        if block:
            blocks.append(block)
    return "\n".join(blocks)


def _escaped_paragraphs(text: str) -> str:
    blocks = []
    for chunk in re.split(r"\n\s*\n", text):
        if chunk.strip():
            blocks.append(f'<p style="{_p_style()}">{html.escape(chunk.strip())}</p>')
    return "\n".join(blocks)


def _block(lines: list[str], index: int, depth: int) -> tuple[str, int]:
    stripped = lines[index].strip()
    if stripped.startswith("```"):
        return _code_block(lines, index)
    heading = _HEADING.match(stripped)
    if heading is not None:
        return _heading(heading), index + 1
    if stripped.startswith(">"):
        return _quote(lines, index, depth)
    if _BULLET.match(stripped) or _ORDERED.match(stripped):
        return _list(lines, index)
    if _is_table(lines, index):
        return _table(lines, index)
    return _paragraph(lines, index)


def _heading(match: re.Match[str]) -> str:
    level = min(len(match.group(1)), 6)
    text = match.group(2).strip()
    if not text:
        return ""
    size = _HEADING_SIZE[level - 1]
    margin = "0 0 12px" if level == 1 else "18px 0 8px"
    style = font(size, PALETTE["ink"], "1.35") + f";margin:{margin};font-weight:600"
    return f'<h{level} style="{style}">{_inline(text)}</h{level}>'


def _code_block(lines: list[str], index: int) -> tuple[str, int]:
    # The info string is dropped. It would only become a class, and a
    # class is not something the message is allowed to introduce.
    index += 1
    body: list[str] = []
    while index < len(lines) and lines[index].strip() != "```":
        body.append(lines[index])
        index += 1
    if index < len(lines):
        index += 1
    style = css(
        ("font-family", MONO),
        ("font-size", "13px"),
        ("line-height", "1.45"),
        ("color", PALETTE["ink"]),
        *bg(PALETTE["code_bg"]),
        ("margin", "0 0 16px"),
        ("padding", "12px 14px"),
        ("white-space", "pre-wrap"),
        ("word-wrap", "break-word"),
    )
    escaped = html.escape("\n".join(body))
    return f'<pre style="{style}"><code style="{_code_style()}">{escaped}</code></pre>', index


def _quote(lines: list[str], index: int, depth: int) -> tuple[str, int]:
    collected: list[str] = []
    while index < len(lines) and lines[index].lstrip().startswith(">"):
        collected.append(_strip_quote(lines[index]))
        index += 1
    inner = render_markdown("\n".join(collected), depth=depth + 1)
    style = css(
        ("margin", "0 0 16px"),
        ("padding", "0 0 0 14px"),
        ("border-left", f"3px solid {PALETTE['quote']}"),
        ("color", PALETTE["muted"]),
    )
    return f'<blockquote style="{style}">{inner}</blockquote>', index


def _strip_quote(line: str) -> str:
    text = line.lstrip()[1:]
    if text.startswith(" "):
        return text[1:]
    return text


def _list(lines: list[str], index: int) -> tuple[str, int]:
    ordered = _ORDERED.match(lines[index].strip()) is not None
    pattern = _ORDERED if ordered else _BULLET
    items: list[str] = []
    while index < len(lines) and lines[index].strip():
        match = pattern.match(lines[index].strip())
        if match is None:
            break
        text = match.group(2) if ordered else match.group(1)
        index += 1
        while index < len(lines) and _continues_item(lines[index], pattern):
            text = f"{text} {lines[index].strip()}"
            index += 1
        item = font("16px", PALETTE["ink"], "1.6") + ";margin:0 0 6px"
        items.append(f'<li style="{item}">{_inline(text)}</li>')
    tag = "ol" if ordered else "ul"
    style = font("16px", PALETTE["ink"], "1.6") + ";margin:0 0 16px;padding:0 0 0 22px"
    return f'<{tag} style="{style}">{"".join(items)}</{tag}>', index


def _continues_item(line: str, pattern: re.Pattern[str]) -> bool:
    if not line.strip() or pattern.match(line.strip()):
        return False
    if _HEADING.match(line.strip()) or line.strip().startswith(("```", ">")):
        return False
    return line.startswith((" ", "\t"))


def _is_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    if "|" not in lines[index] or "|" not in lines[index + 1]:
        return False
    header = _split_row(lines[index])
    separator = _split_row(lines[index + 1])
    if not header or len(header) != len(separator):
        return False
    return all(_SEP_CELL.fullmatch(cell) for cell in separator)


def _is_separator_line(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _split_row(line)
    return bool(cells) and all(_SEP_CELL.fullmatch(cell) for cell in cells)


def _table(lines: list[str], index: int) -> tuple[str, int]:
    header = _split_row(lines[index])
    aligns = [_align(cell) for cell in _split_row(lines[index + 1])]
    index += 2
    rows: list[list[str]] = []
    while index < len(lines):
        line = lines[index]
        if not line.strip() or "|" not in line or _is_separator_line(line):
            break
        rows.append(_split_row(line))
        index += 1
    width = len(header)
    head = "".join(
        _cell("th", text, _align_at(aligns, col), PALETTE["header_cell"])
        for col, text in enumerate(header)
    )
    body_rows: list[str] = []
    for row_index, row in enumerate(rows):
        padded = (row + [""] * width)[:width]
        color = PALETTE["zebra"] if row_index % 2 == 1 else PALETTE["card"]
        cells = "".join(
            _cell("td", text, _align_at(aligns, col), color) for col, text in enumerate(padded)
        )
        body_rows.append(f"<tr>{cells}</tr>")
    table_style = css(
        ("width", "100%"),
        ("border-collapse", "collapse"),
        ("margin", "0 0 16px"),
    )
    body = f"<tbody>{''.join(body_rows)}</tbody>" if body_rows else ""
    return (
        f'<table cellpadding="0" cellspacing="0" border="0" style="{table_style}">'
        f"<thead><tr>{head}</tr></thead>{body}</table>"
    ), index


def _cell(tag: str, text: str, align: str, background: str) -> str:
    weight = ";font-weight:600" if tag == "th" else ""
    style = (
        font("14px", PALETTE["ink"], "1.45")
        + f";margin:0;padding:8px 10px;text-align:{align};vertical-align:top"
        + f";border:1px solid {PALETTE['line']};word-wrap:break-word"
        + f";background-color:{background};background:{background}{weight}"
    )
    return f'<{tag} style="{style}">{_inline(text)}</{tag}>'


def _align(cell: str) -> str:
    left = cell.startswith(":")
    right = cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    return "left"


def _align_at(aligns: list[str], index: int) -> str:
    if index < len(aligns):
        return aligns[index]
    return "left"


def _split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for char in text:
        if escaped:
            buf.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "|":
            cells.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    cells.append("".join(buf).strip())
    return cells


def _paragraph(lines: list[str], index: int) -> tuple[str, int]:
    parts = [lines[index].strip()]
    index += 1
    while index < len(lines) and lines[index].strip() and not _breaks_paragraph(lines, index):
        parts.append(lines[index].strip())
        index += 1
    return f'<p style="{_p_style()}">{_inline(" ".join(parts))}</p>', index


def _breaks_paragraph(lines: list[str], index: int) -> bool:
    stripped = lines[index].strip()
    if stripped.startswith("```") or stripped.startswith(">"):
        return True
    if _HEADING.match(stripped) or _BULLET.match(stripped) or _ORDERED.match(stripped):
        return True
    return _is_table(lines, index)


def _p_style() -> str:
    return font("16px", PALETTE["ink"], "1.6") + ";margin:0 0 16px"


def _code_style() -> str:
    return css(
        ("font-family", MONO),
        ("font-size", "0.92em"),
        ("color", PALETTE["ink"]),
        *bg(PALETTE["code_bg"]),
        ("padding", "1px 4px"),
    )


def _inline(text: str, depth: int = 0) -> str:
    # Late import: email_inline reads this module's palette, and importing
    # it up here would cycle while this file is still loading.
    from .email_inline import render_inline

    return render_inline(text, depth)
