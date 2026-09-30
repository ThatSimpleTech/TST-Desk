"""RRULE expansion for a local calendar file (TD-3818).

DAILY, WEEKLY, and YEARLY with INTERVAL, COUNT, UNTIL, and BYDAY. Any
other frequency is not a rule: the caller keeps the single DTSTART.
The walk is capped so a daily series from decades ago cannot pin a tick.
Past the cap the slot is simply not blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

_BYDAY = re.compile(r"^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$")
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$")
_WEEKDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
# A daily series that starts decades before the slot must not walk every
# day on the tick. Stopping early leaves the slot unblocked.
_WALK_CAP = 20000

Freq = Literal["DAILY", "WEEKLY", "YEARLY"]


@dataclass(frozen=True)
class Byday:
    ordinal: int | None
    weekday: int


@dataclass(frozen=True)
class Rule:
    freq: Freq
    interval: int
    count: int | None
    until_date: date | None
    until_instant: datetime | None
    byday: tuple[Byday, ...]
    bymonth: tuple[int, ...]


def parse_rule(raw: str) -> Rule | None:
    """A supported rule, or None so the caller keeps the base instance.

    A bad INTERVAL, COUNT, or UNTIL drops the whole rule. The first
    DTSTART still stands, which is the same fail-open an unknown
    frequency gets.
    """
    parts: dict[str, str] = {}
    for item in raw.split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts[key.strip().upper()] = value.strip()
    freq = _freq(parts.get("FREQ", ""))
    if freq is None:
        return None
    if "INTERVAL" in parts:
        interval = _positive(parts.get("INTERVAL"))
        if interval is None:
            return None
    else:
        interval = 1
    count = _positive(parts.get("COUNT")) if "COUNT" in parts else None
    if "COUNT" in parts and count is None:
        return None
    until_date, until_instant = _parse_until(parts.get("UNTIL"))
    if "UNTIL" in parts and until_date is None and until_instant is None:
        return None
    return Rule(
        freq=freq,
        interval=interval,
        count=count,
        until_date=until_date,
        until_instant=until_instant,
        byday=_parse_byday(parts.get("BYDAY", "")),
        bymonth=_parse_bymonth(parts.get("BYMONTH", "")),
    )


def occurrence_dates(
    *,
    start: date,
    length_days: int,
    all_day: bool,
    rule: Rule | None,
    slot_date: date,
) -> list[date]:
    """Start dates of instances that could still cover ``slot_date``.

    All-day coverage ends the day before DTEND, so the lookback is one
    less than the length. A timed event can still be in progress on the
    morning of its end date, which is ``length_days`` after the start.
    """
    if rule is None:
        if start <= slot_date:
            return [start]
        return []
    lookback = max(length_days - 1, 0) if all_day else max(length_days, 0)
    earliest = slot_date - timedelta(days=lookback)
    # COUNT is an index from the first instance. Jumping to the slot
    # would number the wrong occurrence and could skip a real one.
    window = start if rule.count is not None else max(start, earliest)
    found: list[date] = []
    for produced, day in enumerate(_walk(start, rule, window, slot_date)):
        if _day_past_until(day, rule):
            break
        if rule.count is not None and produced >= rule.count:
            break
        if day >= earliest:
            found.append(day)
    return found


def _freq(raw: str) -> Freq | None:
    name = raw.strip().upper()
    if name == "DAILY":
        return "DAILY"
    if name == "WEEKLY":
        return "WEEKLY"
    if name == "YEARLY":
        return "YEARLY"
    return None


def _positive(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number < 1:
        return None
    return number


def _parse_until(raw: str | None) -> tuple[date | None, datetime | None]:
    if raw is None:
        return None, None
    value = raw.strip()
    date_match = _DATE.fullmatch(value)
    if date_match is not None:
        return date(int(date_match[1]), int(date_match[2]), int(date_match[3])), None
    dt_match = _DT.fullmatch(value)
    # A datetime UNTIL is UTC. A floating one is not a bound we can trust,
    # so the rule is dropped and the base instance remains.
    if dt_match is None or not value.endswith("Z"):
        return None, None
    instant = datetime(
        int(dt_match[1]),
        int(dt_match[2]),
        int(dt_match[3]),
        int(dt_match[4]),
        int(dt_match[5]),
        int(dt_match[6]),
        tzinfo=UTC,
    )
    return None, instant


def _parse_byday(raw: str) -> tuple[Byday, ...]:
    found: list[Byday] = []
    for token in raw.split(","):
        match = _BYDAY.fullmatch(token.strip().upper())
        if match is None:
            continue
        ordinal = int(match.group(1)) if match.group(1) else None
        if ordinal == 0 or (ordinal is not None and not -5 <= ordinal <= 5):
            continue
        weekday = _WEEKDAY.get(match.group(2))
        if weekday is None:
            continue
        found.append(Byday(ordinal, weekday))
    return tuple(found)


def _parse_bymonth(raw: str) -> tuple[int, ...]:
    found: list[int] = []
    for token in raw.split(","):
        text = token.strip()
        if text.isdigit() and 1 <= int(text) <= 12:
            found.append(int(text))
    return tuple(found)


def _day_past_until(day: date, rule: Rule) -> bool:
    # The date bound is inclusive. A datetime bound is applied again to
    # the instance's start instant by the caller; this only stops the walk.
    if rule.until_date is not None and day > rule.until_date:
        return True
    return rule.until_instant is not None and day > rule.until_instant.astimezone(UTC).date()


def _walk(start: date, rule: Rule, first: date, last: date) -> list[date]:
    if rule.freq == "DAILY":
        return _walk_daily(start, rule, first, last)
    if rule.freq == "WEEKLY":
        return _walk_weekly(start, rule, first, last)
    return _walk_yearly(start, rule, first, last)


def _walk_daily(start: date, rule: Rule, first: date, last: date) -> list[date]:
    if last < start:
        return []
    begin = max(start, first)
    interval = rule.interval
    weekdays = _plain_weekdays(rule.byday)
    delta = (begin - start).days
    day = start + timedelta(days=(delta // interval) * interval)
    if day < begin:
        day += timedelta(days=interval)
    found: list[date] = []
    steps = 0
    while day <= last and steps < _WALK_CAP:
        steps += 1
        if weekdays is None or day.weekday() in weekdays:
            found.append(day)
        day += timedelta(days=interval)
    return found


def _walk_weekly(start: date, rule: Rule, first: date, last: date) -> list[date]:
    if last < start:
        return []
    # An ordinal (1MO) is the weekday. Weekly expansion does not pick the
    # nth Monday of the month; that reading belongs to YEARLY.
    weekdays = _plain_weekdays(rule.byday) or (start.weekday(),)
    week0 = start - timedelta(days=start.weekday())
    begin = max(start, first)
    weeks = (begin - week0).days // 7
    index = max(0, (weeks // rule.interval) * rule.interval)
    found: list[date] = []
    steps = 0
    while steps < _WALK_CAP:
        steps += 1
        monday = week0 + timedelta(weeks=index)
        if monday > last:
            break
        for weekday in sorted(weekdays):
            day = monday + timedelta(days=weekday)
            if day < start or day < first:
                continue
            if day > last:
                return found
            found.append(day)
        index += rule.interval
    return found


def _walk_yearly(start: date, rule: Rule, first: date, last: date) -> list[date]:
    if last < start:
        return []
    begin_year = max(start.year, first.year)
    delta = begin_year - start.year
    year = start.year + (delta // rule.interval) * rule.interval
    if year < begin_year:
        year += rule.interval
    found: list[date] = []
    steps = 0
    while year <= last.year and steps < _WALK_CAP:
        steps += 1
        for day in _days_in_year(start, year, rule):
            if day < start or day < first:
                continue
            if day > last:
                return found
            found.append(day)
        year += rule.interval
    return found


def _days_in_year(start: date, year: int, rule: Rule) -> list[date]:
    if not rule.byday:
        try:
            return [start.replace(year=year)]
        except ValueError:
            # Feb 29 has no anniversary in a non-leap year. Skip the year.
            return []
    months = rule.bymonth or (start.month,)
    found: list[date] = []
    ordinals = [spec for spec in rule.byday if spec.ordinal is not None]
    plain = {spec.weekday for spec in rule.byday if spec.ordinal is None}
    if ordinals:
        for month in months:
            for spec in ordinals:
                if spec.ordinal is None:
                    continue
                day = _nth_weekday(year, month, spec.ordinal, spec.weekday)
                if day is not None:
                    found.append(day)
    # Without an ordinal, BYDAY only keeps the anniversary when that
    # year's month-day falls on one of those weekdays. It does not
    # invent the other 51 weeks.
    if plain and not ordinals:
        try:
            anniversary = date(year, start.month, start.day)
        except ValueError:
            anniversary = None
        if anniversary is not None and anniversary.weekday() in plain:
            found.append(anniversary)
    return sorted(set(found))


def _nth_weekday(year: int, month: int, ordinal: int, weekday: int) -> date | None:
    if ordinal == 0 or not -5 <= ordinal <= 5:
        return None
    if month < 1 or month > 12:
        return None
    if ordinal > 0:
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        day_num = 1 + delta + (ordinal - 1) * 7
        if day_num > _month_length(year, month):
            return None
        return date(year, month, day_num)
    last_num = _month_length(year, month)
    last = date(year, month, last_num)
    delta = (last.weekday() - weekday) % 7
    day_num = last_num - delta + (ordinal + 1) * 7
    if day_num < 1:
        return None
    return date(year, month, day_num)


def _month_length(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - timedelta(days=1)).day
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _plain_weekdays(specs: tuple[Byday, ...]) -> tuple[int, ...] | None:
    """Weekday numbers. Empty means every day. Ordinals are ignored."""
    if not specs:
        return None
    return tuple(spec.weekday for spec in specs)
