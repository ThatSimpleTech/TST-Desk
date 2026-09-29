"""A scheduled job's model pin (TD-3812).

The job stores a catalog preset name and an engine kind, nothing else.
Slugs, URLs, and keys stay in config and the keychain. None means the
run uses whatever the window is using at fire time.

The turn callback is still ``(workspace, instruction)``. The pin is bound
on the task for that call so ``run_turn_on_daemon`` can open the session
on it without the callback growing a parameter every fake would have to
accept, and without touching the daemon's active preset or engine.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..logging import redact_secrets

if TYPE_CHECKING:
    from .models import Job

# A catalog key, not a URL or a path. ``tst-default`` and ``vllm`` pass;
# anything with a slash, a colon, or a space does not, and the message
# does not repeat the text (it may have been a secret-shaped string).
_PRESET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

EngineKind = Literal["native", "grok"]


@dataclass(frozen=True)
class ScheduledPin:
    """The pin bound for one fire. Both empty means use the window's current."""

    preset: str | None = None
    engine: EngineKind | None = None


# None, not a shared ScheduledPin(): ContextVar defaults must not be a
# constructed object (ruff B039), and an empty pin is cheap to build.
_PIN: ContextVar[ScheduledPin | None] = ContextVar("tstd_scheduled_pin", default=None)


def bind_scheduled_pin(job: Job) -> Token[ScheduledPin | None]:
    """Bind *job*'s pin for the current task. Reset with the token."""
    return _PIN.set(ScheduledPin(preset=job.preset, engine=job.engine))


def reset_scheduled_pin(token: Token[ScheduledPin | None]) -> None:
    """Drop the pin so the next job on this task does not inherit it."""
    _PIN.reset(token)


def current_scheduled_pin() -> ScheduledPin:
    """The pin bound for this task, or an empty pin when nothing is bound."""
    return _PIN.get() or ScheduledPin()


def normalize_preset(value: str | None) -> str | None:
    """A catalog name, or None when the field was left blank.

    Raises ``ValueError`` with a message that does not echo *value*.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if redact_secrets(text) != text:
        raise ValueError("must not contain secrets")
    if _PRESET_NAME.fullmatch(text) is None:
        raise ValueError("must be a catalog name")
    return text


def normalize_engine(value: str | None) -> EngineKind | None:
    """``native`` or ``grok``, or None when blank. Case is ignored."""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text == "native":
        return "native"
    if text == "grok":
        return "grok"
    raise ValueError("must be native or grok")


def require_known_preset(name: str | None, catalog: Mapping[str, object]) -> None:
    """Refuse a preset the catalog does not have. None means use current.

    Checked only when a save sends a name. A stored name is not re-checked
    on pause, or a job whose preset was removed could not be paused. The
    run reports that removal itself.
    """
    if name is None or name in catalog:
        return
    # Lazy: this module is imported by models, which defines the error.
    from .models import JobValidationError

    raise JobValidationError(f"Preset: '{name}' is not in the catalog")


def pin_failure_summary(
    pin: ScheduledPin,
    *,
    catalog: Mapping[str, object],
    grok_available: bool,
) -> str | None:
    """Why this fire cannot start, or None when the pin is usable.

    A missing preset wins over an unavailable Grok engine: the name is
    what the user pinned, and it is already gone.
    """
    if pin.preset is not None and pin.preset not in catalog:
        return f"preset '{pin.preset}' no longer exists"
    if pin.engine == "grok" and not grok_available:
        return "grok engine is unavailable"
    return None


def grok_cli_available(binary: str) -> bool:
    """Whether the pinned Grok engine can start. The path stays out of the receipt."""
    from ..grok_acp import GrokEngineError, find_grok_binary

    try:
        find_grok_binary(binary)
    except GrokEngineError:
        return False
    return True


async def scheduled_run_block(catalog: Mapping[str, object], *, grok_binary: str) -> str | None:
    """The receipt for a pin that cannot start, or None to open the session.

    The Grok probe runs only when the job pinned Grok. A window that is
    on Grok while the job pinned native is not probed.
    """
    pin = current_scheduled_pin()
    grok_ok = True
    if pin.engine == "grok":
        grok_ok = await asyncio.to_thread(grok_cli_available, grok_binary)
    return pin_failure_summary(pin, catalog=catalog, grok_available=grok_ok)
