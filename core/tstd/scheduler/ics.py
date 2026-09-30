"""Read a local iCalendar file (TD-3818).

No calendar library. A subscription address would be a fetch the user
did not make, so only the text already on disk is read. The parser
covers VEVENT with DTSTART/DTEND (DATE or DATE-TIME, TZID or UTC),
SUMMARY, and the recurrence in ``ics_recur``. Anything else is ignored.
A file that does not parse is unreadable so the job still runs; it is
never treated as a block.

DTEND is exclusive: an all-day holiday's end date is the next morning
and must not block that morning. All-day coverage uses the job's local
date. Timed coverage uses the slot instant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from .ics_lines import Prop, scan_components
from .ics_recur import Rule, occurrence_dates, parse_rule

_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$")


@dataclass(frozen=True)
class Event:
    summary: str
    all_day: bool
    start: date
    # All-day: exclusive length in days. Timed: days from the start date
    # to the end date (0 when both are the same civil day).
    length_days: int
    start_time: time | None
    end_time: time | None
    # None is floating (the job's zone). ``UTC`` is a Z value. Anything
    # else is a TZID that ZoneInfo accepted.
    zone_name: str | None
    rule: Rule | None


@dataclass(frozen=True)
class CalendarFile:
    events: tuple[Event, ...]
    unreadable: bool


@dataclass(frozen=True)
class _Point:
    all_day: bool
    day: date
    clock: datetime | None
    zone_name: str | None


def parse_ics(text: str) -> CalendarFile:
    """Events in ``text``, or unreadable when the file is not a calendar."""
    saw_event = False
    saw_calendar = False
    events: list[Event] = []
    failed = False
    for kind, props in scan_components(text):
        if kind == "VCALENDAR":
            saw_calendar = True
            continue
        saw_event = True
        try:
            event = _event_from(props)
        except (ValueError, KeyError, OSError):
            failed = True
            continue
        events.append(event)
    if events:
        return CalendarFile(tuple(events), False)
    # A calendar with no events is readable and blocks nothing. A file
    # that started an event and never finished one is not a calendar.
    if saw_event or failed or not saw_calendar:
        return CalendarFile((), True)
    return CalendarFile((), False)


def slot_blocked(
    parsed: CalendarFile,
    slot: datetime,
    job_tz: str | None,
    match: str | None,
) -> bool:
    """True when ``slot`` falls in an event whose summary matches.

    The summary is not returned. A caller that records a skip has nothing
    to write except that a calendar matched.
    """
    if parsed.unreadable or not parsed.events:
        return False
    try:
        job_zone: tzinfo = ZoneInfo(job_tz) if job_tz else UTC
    except (KeyError, ValueError, OSError):
        return False
    when = slot if slot.tzinfo is not None else slot.replace(tzinfo=UTC)
    when = when.astimezone(UTC)
    terms = _terms(match)
    for event in parsed.events:
        if not _summary_matches(event.summary, terms):
            continue
        try:
            if _covers(event, when, job_zone):
                return True
        except (KeyError, ValueError, OSError):
            continue
    return False


def _covers(event: Event, slot: datetime, job_zone: tzinfo) -> bool:
    if event.all_day:
        slot_date = slot.astimezone(job_zone).date()
        for day in _dates(event, slot_date):
            end = day + timedelta(days=event.length_days)
            if day <= slot_date < end:
                return True
        return False
    zone = _zone(event, job_zone)
    slot_date = slot.astimezone(zone).date()
    for day in _dates(event, slot_date):
        window = _window(event, day, zone)
        if window is None:
            continue
        start, end = window
        until = event.rule.until_instant if event.rule is not None else None
        if until is not None and start > until:
            break
        if start == end:
            if slot == start:
                return True
        elif start <= slot < end:
            return True
    return False


def _dates(event: Event, slot_date: date) -> list[date]:
    return occurrence_dates(
        start=event.start,
        length_days=event.length_days,
        all_day=event.all_day,
        rule=event.rule,
        slot_date=slot_date,
    )


def _window(event: Event, day: date, zone: tzinfo) -> tuple[datetime, datetime] | None:
    if event.start_time is None or event.end_time is None:
        return None
    start_wall = datetime.combine(day, event.start_time)
    end_day = day + timedelta(days=event.length_days)
    end_wall = datetime.combine(end_day, event.end_time)
    start = _localize(start_wall, zone)
    end = _localize(end_wall, zone)
    if start is None or end is None:
        return None
    return start, end


def _zone(event: Event, job_zone: tzinfo) -> tzinfo:
    if event.zone_name is None:
        return job_zone
    if event.zone_name == "UTC":
        return UTC
    return ZoneInfo(event.zone_name)


def _localize(wall: datetime, zone: tzinfo) -> datetime | None:
    """UTC instant for a naive wall time, or None if that time never happened.

    The spring-forward gap is not a real instant. Skipping it leaves the
    slot unblocked for that one occurrence rather than shifting it onto
    a neighboring hour.
    """
    naive = wall.replace(tzinfo=None)
    local = naive.replace(tzinfo=zone)
    instant = local.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return instant


def _terms(match: str | None) -> tuple[str, ...] | None:
    if match is None or not match.strip():
        return None
    parts = tuple(part.strip().casefold() for part in match.split("|") if part.strip())
    return parts or None


def _summary_matches(summary: str, terms: tuple[str, ...] | None) -> bool:
    if terms is None:
        return True
    folded = summary.casefold()
    return any(term in folded for term in terms)


def _event_from(props: dict[str, Prop]) -> Event:
    if "DTSTART" not in props:
        raise ValueError("unreadable")
    start_params, start_raw = props["DTSTART"]
    start = _parse_point(start_raw, start_params)
    end: _Point | None = None
    if "DTEND" in props:
        end_params, end_raw = props["DTEND"]
        end = _parse_point(end_raw, end_params)
        if end.all_day != start.all_day:
            raise ValueError("unreadable")
        if not start.all_day and end.zone_name not in {None, start.zone_name}:
            raise ValueError("unreadable")
    summary = ""
    if "SUMMARY" in props:
        summary = _unescape(props["SUMMARY"][1]).strip()
    rule = parse_rule(props["RRULE"][1]) if "RRULE" in props else None
    if start.all_day:
        end_day = end.day if end is not None else start.day + timedelta(days=1)
        if end_day <= start.day:
            end_day = start.day + timedelta(days=1)
        return Event(
            summary=summary,
            all_day=True,
            start=start.day,
            length_days=(end_day - start.day).days,
            start_time=None,
            end_time=None,
            zone_name=None,
            rule=rule,
        )
    if start.clock is None:
        raise ValueError("unreadable")
    end_clock = end.clock if end is not None and end.clock is not None else start.clock
    end_day = end.day if end is not None else start.day
    offset = (end_day - start.day).days
    if offset < 0:
        raise ValueError("unreadable")
    if offset == 0 and end_clock <= start.clock:
        end_clock = start.clock
    return Event(
        summary=summary,
        all_day=False,
        start=start.day,
        length_days=offset,
        start_time=start.clock.time(),
        end_time=end_clock.time(),
        zone_name=start.zone_name,
        rule=rule,
    )


def _parse_point(raw: str, params: dict[str, str]) -> _Point:
    value = raw.strip()
    kind = params.get("VALUE", "").upper()
    date_match = _DATE.fullmatch(value)
    if kind == "DATE" or (kind != "DATE-TIME" and date_match is not None):
        if date_match is None:
            raise ValueError("unreadable")
        day = date(int(date_match[1]), int(date_match[2]), int(date_match[3]))
        return _Point(True, day, None, None)
    dt_match = _DT.fullmatch(value)
    if dt_match is None:
        raise ValueError("unreadable")
    clock = datetime(
        int(dt_match[1]),
        int(dt_match[2]),
        int(dt_match[3]),
        int(dt_match[4]),
        int(dt_match[5]),
        int(dt_match[6]),
    )
    if value.endswith("Z"):
        zone: str | None = "UTC"
    elif params.get("TZID"):
        zone = _valid_zone(params["TZID"])
    else:
        zone = None
    return _Point(False, clock.date(), clock, zone)


def _valid_zone(tzid: str) -> str:
    # Some exports prefix the IANA name with a product path. The last two
    # segments are the zone when the whole string is not one.
    candidates = [tzid]
    parts = [part for part in tzid.split("/") if part]
    if len(parts) >= 2:
        candidates.append("/".join(parts[-2:]))
    for name in candidates:
        try:
            ZoneInfo(name)
        except (KeyError, ValueError, OSError):
            continue
        return name
    raise ValueError("unreadable")


def _unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            mapped = {"n": "\n", "N": "\n", "\\": "\\", ",": ",", ";": ";"}.get(nxt, nxt)
            out.append(mapped)
            index += 2
            continue
        out.append(value[index])
        index += 1
    return "".join(out)
