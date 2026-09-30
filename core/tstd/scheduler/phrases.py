"""Plain-English cadences, converted to the 5-field cron the store keeps.

Users type "7:45 on weekdays", not ``45 7 * * 1-5``. Everything here ends in
cron so the store, runner and UI only ever see an interval or a cron string;
this module is the only place that knows English.

The grammar is a bag of tokens, not a sentence: fillers (at/on/every/and),
day words, and at most one clock time, in any order. That is deliberately
looser than a real parser — "at 9am every day" and "every day at 9am" must
both work, and a bag needs no rule per word order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

# Cron numbers Sunday as 0; Saturday and Sunday are 6 and 0, not adjacent.
_WEEKDAY_NUMBERS = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 0,
}
_WEEKDAY_SPELLINGS = {
    **_WEEKDAY_NUMBERS,
    "mon": 1,
    "tue": 2,
    "tues": 2,
    "wed": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "fri": 5,
    "sat": 6,
    "sun": 0,
}
_MON_TO_FRI = frozenset({1, 2, 3, 4, 5})
_SAT_AND_SUN = frozenset({0, 6})

_NAMED_DAY = "|".join(sorted(_WEEKDAY_SPELLINGS, key=len, reverse=True))
# A bare "day" is not a token, only "every day": otherwise "on the day of the
# release" inside a sentence would read as a daily schedule.
_TOKEN = re.compile(
    rf"""(?P<alldays>\b(?:(?:every|each)\s+day|daily)\b)
    |(?P<filler>\b(?:at|on|every|each|and|the)\b|[,&])
    |(?P<hourly>\bhourly\b)
    |(?P<weekdays>\bweekdays?\b)
    |(?P<weekends>\bweekends?\b)
    |(?P<named>\b(?:{_NAMED_DAY})s?\b)
    |(?P<word>\b(?:noon|midnight)\b)
    |(?P<clock>\b(?P<h>\d{{1,2}})(?::(?P<m>\d{{2}}))?(?:\s*(?P<ap>[ap]\.?m\.?))?)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_ALIAS = re.compile(r"(?<!\S)@(?:hourly|daily|midnight|weekly|monthly)(?!\S)", re.IGNORECASE)
# A token must end at whitespace or a separator, so "9 amazing" is not 9 am.
_TOKEN_END = re.compile(r"(?=[\s,&]|$)")
_SEPARATOR = re.compile(r"[\s,]*")
_LEADING_GLUE = frozenset({"and", "the", ",", "&"})
_DAY_KINDS = ("alldays", "hourly", "weekdays", "weekends", "named")


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int
    # (hour, minute) for a clock time; None means the digits were impossible.
    time: tuple[int, int] | None = None
    explicit: bool = False


def expand_alias(text: str) -> str | None:
    """``@daily`` and friends as cron, or None."""
    return ALIASES.get(text.strip().lower())


def phrase_to_cron(text: str) -> str | None:
    """A whole cadence phrase as 5-field cron, or None when it is not one."""
    tokens = _scan(text, 0)
    if not tokens or tokens[-1].end != len(text.rstrip()):
        return None
    return _interpret(tokens, embedded=False)


def find_phrase(text: str) -> tuple[int, int, str] | None:
    """The first cadence phrase inside a longer sentence, as (start, end, cron).

    The span lets the caller cut the phrase out of the instruction. Stricter
    than ``phrase_to_cron``: a lone bare number ("at 3") is not enough, since
    sentences are full of them.
    """
    alias = _ALIAS.search(text)
    if alias is not None:
        return alias.start(), alias.end(), ALIASES[alias.group(0).lower()]
    for word in re.finditer(r"\S+", text):
        tokens = _trim(_scan(text, word.start()))
        cron = _interpret(tokens, embedded=True) if tokens else None
        if cron is not None:
            return tokens[0].start, tokens[-1].end, cron
    return None


def _scan(text: str, pos: int) -> list[_Token]:
    """The longest run of cadence tokens starting at ``pos``."""
    tokens: list[_Token] = []
    while True:
        separator = _SEPARATOR.match(text, pos)
        pos = separator.end() if separator is not None else pos
        match = _TOKEN.match(text, pos)
        if match is None or _TOKEN_END.match(text, match.end()) is None:
            return tokens
        token = _make_token(match, tokens)
        if token is None:
            return tokens
        tokens.append(token)
        pos = match.end()


def _make_token(match: re.Match[str], before: list[_Token]) -> _Token | None:
    text = match.group(0).lower()
    start, end = match.span()
    if match.group("filler"):
        return _Token("filler", text, start, end)
    for kind in _DAY_KINDS:
        if match.group(kind):
            return _Token(kind, text, start, end)
    if match.group("word"):
        return _Token("time", text, start, end, (12 if text == "noon" else 0, 0), explicit=True)
    return _clock_token(match, before)


def _clock_token(match: re.Match[str], before: list[_Token]) -> _Token | None:
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    meridiem = (match.group("ap") or "").replace(".", "").lower()
    explicit = match.group("m") is not None or bool(meridiem)
    # A bare number is only a time after "at": "every 2" is not 02:00.
    if not explicit and not (before and before[-1].text == "at"):
        return None
    start, end = match.span()
    text = match.group(0).lower()
    valid = minute <= 59 and (1 <= hour <= 12 if meridiem else hour <= 23)
    if not valid:
        return _Token("time", text, start, end, None, explicit)
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return _Token("time", text, start, end, (hour, minute), explicit)


def _trim(tokens: list[_Token]) -> list[_Token]:
    """Drop fillers that are sentence glue rather than part of the cadence."""
    while tokens and tokens[-1].kind == "filler":
        tokens = tokens[:-1]
    while tokens and tokens[0].kind == "filler" and tokens[0].text in _LEADING_GLUE:
        tokens = tokens[1:]
    return tokens


def _interpret(tokens: list[_Token], *, embedded: bool) -> str | None:
    content = [token for token in tokens if token.kind != "filler"]
    times = [token for token in content if token.kind == "time"]
    days = [token for token in content if token.kind != "time"]
    if not content or len(times) > 1:
        return None
    clock = times[0].time if times else (0, 0)
    if clock is None:
        return None
    if embedded and not days and not times[0].explicit:
        return None
    weekdays = _weekday_field(days)
    if any(token.kind == "hourly" for token in days):
        return None if times else f"0 * * * {weekdays}"
    return f"{clock[1]} {clock[0]} * * {weekdays}"


def _weekday_field(days: list[_Token]) -> str:
    """Cron day-of-week field for the day words, ``*`` when none restrict."""
    chosen: set[int] = set()
    for token in days:
        if token.kind == "alldays":
            return "*"
        if token.kind == "weekdays":
            chosen |= _MON_TO_FRI
        elif token.kind == "weekends":
            chosen |= _SAT_AND_SUN
        elif token.kind == "named":
            chosen.add(_named_day(token.text))
    if not chosen or len(chosen) == 7:
        return "*"
    return _compress(sorted(chosen))


def _named_day(text: str) -> int:
    """Cron number for "mon", "mondays", "tues"; a plural "s" is optional."""
    if text in _WEEKDAY_SPELLINGS:
        return _WEEKDAY_SPELLINGS[text]
    return _WEEKDAY_SPELLINGS[text[:-1]]


def _compress(numbers: list[int]) -> str:
    """``[1,2,3,4,5]`` -> ``1-5``; runs shorter than three stay a plain list."""
    parts: list[str] = []
    index = 0
    while index < len(numbers):
        end = index
        while end + 1 < len(numbers) and numbers[end + 1] == numbers[end] + 1:
            end += 1
        if end - index >= 2:
            parts.append(f"{numbers[index]}-{numbers[end]}")
        else:
            parts.extend(str(n) for n in numbers[index : end + 1])
        index = end + 1
    return ",".join(parts)
