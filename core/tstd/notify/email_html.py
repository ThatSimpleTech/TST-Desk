"""Newsletter shell for a scheduled report (TD-3821).

One centred column, inline styles, a header band, and a footer. The
body comes from ``email_markdown``, which escapes the model text. This
module does not introduce a second palette.
"""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from .email_markdown import FONT, PALETTE, bg, css, font, render_markdown

_COLUMN = "680px"


def local_when(when: datetime, timezone_name: str | None) -> datetime:
    """*when* in the job's zone, or the machine zone when that name is unknown."""
    if timezone_name:
        try:
            return when.astimezone(ZoneInfo(timezone_name))
        except (KeyError, ValueError, OSError):
            pass
    return when.astimezone()


def render_report_html(
    markdown: str,
    *,
    title: str,
    when: datetime,
    timezone_name: str | None,
    preset: str,
) -> str:
    """A full HTML document for *markdown*. *title* and *preset* are escaped."""
    local = local_when(when, timezone_name)
    safe_title = html.escape((title or "").strip() or "Scheduled job", quote=True)
    safe_preset = html.escape((preset or "").strip() or "current", quote=True)
    safe_date = html.escape(_long_date(local), quote=True)
    footer = f"Sent by TST Desk · {safe_title} · ran {_clock(local)} on {safe_preset}"
    body = render_markdown(markdown)
    return _document(safe_title, safe_date, body, footer)


def _long_date(local: datetime) -> str:
    return f"{local.strftime('%B')} {local.day}, {local.year}"


def _clock(local: datetime) -> str:
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}"


def _document(title: str, date: str, body: str, footer: str) -> str:
    page = css(("width", "100%"), ("margin", "0"), ("padding", "0"), *bg(PALETTE["page"]))
    card = css(("width", "100%"), ("max-width", _COLUMN), *bg(PALETTE["card"]))
    band = css(("padding", "22px 32px"), *bg(PALETTE["band"]))
    title_style = font("22px", PALETTE["band_ink"], "1.3") + ";margin:0;font-weight:600"
    date_style = font("13px", PALETTE["band_muted"], "1.4") + ";margin:6px 0 0"
    content = css(
        ("padding", "28px 32px 8px"),
        ("font-family", FONT),
        ("font-size", "16px"),
        ("line-height", "1.6"),
        ("color", PALETTE["ink"]),
        *bg(PALETTE["card"]),
    )
    footer_cell = css(
        ("padding", "8px 32px 22px"),
        ("border-top", f"1px solid {PALETTE['line']}"),
        *bg(PALETTE["card"]),
    )
    footer_style = font("12px", PALETTE["muted"], "1.5") + ";margin:0"
    gutter = css(("padding", "24px 12px"), *bg(PALETTE["page"]))
    lines = [
        "<!DOCTYPE html>",
        "<html>",
        '<head><meta charset="utf-8"></head>',
        f'<body style="{page}">',
        (
            '<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0" style="{page}">'
        ),
        "<tr>",
        f'<td align="center" style="{gutter}">',
        (
            '<table role="presentation" width="680" cellpadding="0" '
            f'cellspacing="0" border="0" style="{card}">'
        ),
        "<tr>",
        f'<td style="{band}">',
        f'<p style="{title_style}">{title}</p>',
        f'<p style="{date_style}">{date}</p>',
        "</td>",
        "</tr>",
        "<tr>",
        f'<td style="{content}">',
        body,
        "</td>",
        "</tr>",
        "<tr>",
        f'<td style="{footer_cell}">',
        f'<p style="{footer_style}">{footer}</p>',
        "</td>",
        "</tr>",
        "</table>",
        "</td>",
        "</tr>",
        "</table>",
        "</body>",
        "</html>",
    ]
    return "\n".join(lines) + "\n"
