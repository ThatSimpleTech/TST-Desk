"""Reasoning effort: the picker's Low..Max, and the Ultracode switch's legacy name.

One level per session, picked in the composer. It rides on the request as
the OpenAI-compatible ``reasoning_effort`` field. A tier takes it only when
its config says ``reasoning_effort: true`` (a gateway or provider that
supports the field), so a server that rejects unknown fields never sees it.

Wire names are the contract with the gateway: low | medium | high | xhigh
| max. The picker's "Extra" is ``xhigh``.

Ultracode used to be a sixth level (TD-5195). It is now a per-session
switch that works with any level. ``ultracode`` is still accepted on input
(an older client, a stored session, an old config) and means Max with the
switch on. It is never sent as ``reasoning_effort``.
"""

from __future__ import annotations

from typing import Any, Literal, TypeGuard, get_args

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]

EFFORT_LEVELS: tuple[str, ...] = get_args(EffortLevel)

#: The machine-wide default for a session that never picked one.
DEFAULT_EFFORT: EffortLevel = "medium"

#: The old sixth level. Accepted on input only: it means Max with Ultracode on.
LEGACY_ULTRACODE = "ultracode"

#: How a stored effort says the switch is on ("high+ultracode"). The switch
#: rides in the session row's one effort string rather than a key of its
#: own: an older build drops a whole row with a key it does not know, and
#: reads this string as an unknown level instead, keeping the row.
ULTRACODE_SUFFIX = "+ultracode"


def is_effort_level(value: object) -> TypeGuard[EffortLevel]:
    """Whether *value* is one of the five wire levels."""
    return isinstance(value, str) and value in EFFORT_LEVELS


def with_effort(
    extra_body: dict[str, Any] | None,
    effort: str,
    *,
    tier_supports: bool,
) -> dict[str, Any] | None:
    """*extra_body* plus ``reasoning_effort`` when the tier takes it.

    The picked level wins over a ``reasoning_effort`` the tier's own
    ``extra_body`` carries: the person chose it for this session. Returns
    the input untouched (not copied) when the tier takes no effort.
    """
    if not tier_supports or not is_effort_level(effort):
        return extra_body
    merged = dict(extra_body or {})
    merged["reasoning_effort"] = effort
    return merged


def split_legacy_effort(value: object) -> tuple[EffortLevel | None, bool]:
    """``(level, ultracode)`` for an incoming effort.

    ``"ultracode"`` is ``("max", True)``; a level is ``(level, False)``;
    anything else is ``(None, False)``.
    """
    if value == LEGACY_ULTRACODE:
        return "max", True
    if is_effort_level(value):
        return value, False
    return None, False


def effort_record(level: EffortLevel, ultracode: bool) -> str:
    """The session row's effort string: ``"high"`` or ``"high+ultracode"``."""
    return level + ULTRACODE_SUFFIX if ultracode else level


def read_effort_record(value: str | None) -> tuple[EffortLevel | None, bool]:
    """``(level, ultracode)`` from a session row's effort string.

    Reads ``"high+ultracode"``, a plain level and the legacy ``"ultracode"``.
    None or anything else is ``(None, False)``: the caller uses its defaults.
    """
    if value is None:
        return None, False
    if value.endswith(ULTRACODE_SUFFIX):
        level = value[: -len(ULTRACODE_SUFFIX)]
        return (level, True) if is_effort_level(level) else (None, False)
    return split_legacy_effort(value)
