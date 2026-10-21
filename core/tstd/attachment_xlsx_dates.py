"""Dates in an XLSX attachment, read as dates (TD-5161).

A spreadsheet stores a date as a number (days since the workbook's epoch)
and says it is a date only through the cell's number format. Sent raw, a
date column reads as ``45583``; these helpers find the date formats in
``xl/styles.xml`` and turn such a cell into ``2024-10-18``.

A leaf module: it takes the parsed XML events, so the zip and XML safety
checks stay in :mod:`tstd.attachment_office`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: Excel's built-in date and time formats (ECMA-376 18.8.30, plus the
#: East Asian ids Excel writes for its localized date formats).
BUILTIN_DATE_FORMATS = frozenset({*range(14, 23), *range(27, 37), 45, 46, 47, *range(50, 59)})

# Quoted text, escaped characters and [colour]/[$-locale] sections say
# nothing about dates; [h] [mm] [ss] (elapsed time) do.
_NOISE = re.compile(r'"[^"]*"|\\.|_.|\*.|\[(?![hms]+\])[^\]]*\]', re.IGNORECASE)
_DATE_LETTERS = re.compile(r"[dmyhs]", re.IGNORECASE)

_EPOCH_1900 = datetime(1899, 12, 30)  # absorbs Excel's 1900 leap-year bug
_EPOCH_1904 = datetime(1904, 1, 1)


@dataclass(frozen=True)
class DateStyles:
    """Which cell styles show a date, and the workbook's epoch."""

    styles: frozenset[int]
    epoch_1904: bool


def is_date_format(code: str) -> bool:
    """Whether a custom number format shows a date or a time."""
    return bool(_DATE_LETTERS.search(_NOISE.sub("", code)))


def date_styles(events: Iterable[tuple[str, str, Any]]) -> frozenset[int]:
    """The ``cellXfs`` indexes (a cell's ``s``) whose format is a date or time."""
    custom: dict[int, bool] = {}
    styles: list[int] = []
    in_xfs = 0
    for event, tag, el in events:
        if tag == "cellXfs":
            in_xfs += 1 if event == "start" else -1
        elif event != "end":
            continue
        elif tag == "numFmt":
            fmt_id = _int(el.get("numFmtId"))
            if fmt_id is not None:
                custom[fmt_id] = is_date_format(el.get("formatCode") or "")
        elif tag == "xf" and in_xfs:
            styles.append(_int(el.get("numFmtId")) or 0)
    return frozenset(
        i for i, fmt_id in enumerate(styles) if custom.get(fmt_id, fmt_id in BUILTIN_DATE_FORMATS)
    )


def as_date(raw: str, *, epoch_1904: bool = False) -> str | None:
    """A serial date as ISO text, or None when *raw* is not a usable number."""
    try:
        serial = float(raw)
    except ValueError:
        return None
    if not math.isfinite(serial) or serial < 0 or serial >= 2_958_466:  # past 9999-12-31
        return None
    whole = math.floor(serial)
    seconds = round((serial - whole) * 86_400)
    if seconds == 86_400:
        whole, seconds = whole + 1, 0
    clock = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    if whole == 0 and not epoch_1904:
        return clock  # a time of day with no date
    day = (_EPOCH_1904 if epoch_1904 else _EPOCH_1900) + timedelta(days=whole)
    return day.strftime("%Y-%m-%d") + (f" {clock}" if seconds else "")


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
