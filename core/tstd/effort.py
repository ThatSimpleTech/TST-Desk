"""Reasoning effort: the picker's Low..Max and Ultracode (the model picker).

One level per session, picked in the composer. It rides on the request as
the OpenAI-compatible ``reasoning_effort`` field. The model ``ezer`` always
takes it; any other tier takes it only when its config says
``reasoning_effort: true`` (a provider that supports the field today), so
a server that rejects unknown fields never sees it.

Wire names are the contract with the gateway: low | medium | high | xhigh
| max | ultracode. The picker's "Extra" is ``xhigh``.
"""

from __future__ import annotations

from typing import Any, Literal, TypeGuard, get_args

EffortLevel = Literal["low", "medium", "high", "xhigh", "max", "ultracode"]

EFFORT_LEVELS: tuple[str, ...] = get_args(EffortLevel)

#: The machine-wide default for a session that never picked one.
DEFAULT_EFFORT: EffortLevel = "medium"

#: The model slug that always takes ``reasoning_effort``.
EZER_SLUG = "ezer"


def is_effort_level(value: object) -> TypeGuard[EffortLevel]:
    """Whether *value* is one of the six wire levels."""
    return isinstance(value, str) and value in EFFORT_LEVELS


def effort_applies(slug: str | None, tier_supports: bool) -> bool:
    """Whether a request to *slug* carries ``reasoning_effort``.

    ``ezer`` always; anything else only when the tier opted in.
    """
    return (slug or "").strip() == EZER_SLUG or tier_supports


def with_effort(
    extra_body: dict[str, Any] | None,
    effort: str,
    *,
    slug: str | None,
    tier_supports: bool,
) -> dict[str, Any] | None:
    """*extra_body* plus ``reasoning_effort`` when the model takes it.

    The picked level wins over a ``reasoning_effort`` the tier's own
    ``extra_body`` carries: the person chose it for this session. Returns
    the input untouched (not copied) when the model takes no effort.
    """
    if not effort_applies(slug, tier_supports) or not is_effort_level(effort):
        return extra_body
    merged = dict(extra_body or {})
    merged["reasoning_effort"] = effort
    return merged
