"""How late a scheduled slot may be and still run (TD-3813).

The tick used to fire every due slot once, however old it was. A laptop
that slept through 7:45 and opened at 18:00 then ran the morning digest
at dinner. ``grace`` is the longest lateness that is still that slot.
None keeps the old behaviour.

Stored as seconds. The phrases people type ("2 hours", "30 minutes",
and the scheduler's "every 2 hours") are the same duration in more than
one spelling, and the comparison is an instant minus a duration. A
string on disk would be parsed again on every tick, and a bad string
would fail the tick instead of the save.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from ..logging import redact_secrets

# Same units the interval cadence accepts. "every" is optional here: a
# grace is a duration, not a schedule, but "every 2 hours" is how the
# scheduler already spells two hours.
_DURATION = re.compile(
    r"^(?:every\s+)?([1-9]\d*)\s+(minutes?|hours?|days?)$",
    re.IGNORECASE,
)
_UNITS = {
    "minute": "minute",
    "minutes": "minute",
    "hour": "hour",
    "hours": "hour",
    "day": "day",
    "days": "day",
}
_UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}
# A typo like "20000 hours" must not become a timedelta the clock cannot
# subtract. A year is longer than any digest is worth running late.
_MAX_SECONDS = 366 * 86400


class GraceError(ValueError):
    """The duration was not a grace we can store. Nothing was written."""


def parse_grace(value: object) -> int | None:
    """Seconds, or None when the field is blank.

    Accepts a positive int (already seconds), a digit string, or one
    plain-English duration. A blank is "always run".
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise GraceError("must be a duration such as '2 hours' or '30 minutes'")
    if isinstance(value, int):
        return _bounded(value)
    if redact_secrets(value) != value:
        # The refusal must not repeat the text. It may be a key.
        raise GraceError("must not contain secrets")
    text = " ".join(value.split())
    if text == "":
        return None
    if text.isdigit():
        return _bounded(int(text))
    match = _DURATION.fullmatch(text)
    if match is None:
        shown = text if len(text) <= 40 else text[:40] + "…"
        raise GraceError(f"not understood: {shown!r}. Try '30 minutes', '2 hours', or '6 hours'")
    count = int(match.group(1))
    stem = _UNITS.get(match.group(2).lower())
    if stem is None:
        raise GraceError(f"not understood: {text!r}. Try '30 minutes', '2 hours', or '6 hours'")
    return _bounded(count * _UNIT_SECONDS[stem])


def past_grace(slot: datetime | None, grace_seconds: int | None, now: datetime) -> bool:
    """True when ``slot`` is strictly older than ``now - grace``.

    Equal lateness still runs. The pane says "more than", and a slot
    that is late by exactly the grace the user picked is still that
    slot. No grace means never skip.
    """
    if slot is None or grace_seconds is None:
        return False
    return _as_utc(now) - _as_utc(slot) > timedelta(seconds=grace_seconds)


def skip_line(slot: datetime, timezone: str | None, now: datetime) -> str:
    """One sentence for the channel, the receipt, and the history line.

    The clock is the slot in the job's zone. A job saved before zones
    existed is UTC, which is the clock that slot was armed in. The
    hour matches the row's cadence words (``7:45 AM``, not ``07:45``).
    """
    clock = _clock(slot, timezone)
    return f"Skipped the {clock} run — {_late_phrase(_as_utc(now) - _as_utc(slot))}"


def _bounded(seconds: int) -> int:
    if seconds <= 0:
        raise GraceError("must be longer than nothing")
    if seconds > _MAX_SECONDS:
        raise GraceError("cannot be longer than 366 days")
    return seconds


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _zone(name: str | None) -> tzinfo:
    if name is None:
        return UTC
    return ZoneInfo(name)


def _clock(slot: datetime, timezone: str | None) -> str:
    local = _as_utc(slot).astimezone(_zone(timezone))
    suffix = "AM" if local.hour < 12 else "PM"
    hour12 = local.hour % 12 or 12
    return f"{hour12}:{local.minute:02d} {suffix}"


def _late_phrase(late: timedelta) -> str:
    """Whole minutes, rounded up, so a skip never claims a shorter delay.

    An exact hour stays ``10 h``. A few seconds past an hour becomes
    the next minute. Under an hour is ``35 min``.
    """
    total = int(late.total_seconds())
    if total < 0:
        total = 0
    minutes = max(1, (total + 59) // 60)
    hours, mins = divmod(minutes, 60)
    if hours == 0:
        return f"{mins} min late"
    if mins == 0:
        return f"{hours} h late"
    return f"{hours} h {mins} min late"
