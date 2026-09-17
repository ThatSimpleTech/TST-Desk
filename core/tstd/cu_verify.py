"""Action-effect verification for computer-use actuation (TD-709, dev build).

Today the only post-actuation check is ``expect_window`` substring
matching (TD-3301) — a guard *before* the click, nothing after it.  This
module adds the after: a judgment on the configured ``JudgmentBackend``
over a compact text before/after state.  Never a screenshot, never raw
page content — the state is a bounded string, so what leaves the machine
is inspectable.

Fail-open by design: a driver that cannot describe its state, a dead
connector, an unparseable answer, or low confidence all yield
``unavailable`` and the tool result is exactly what it would have been
without verification.  Verification may add information (and later,
refusals); it never removes the static ``actuates → B`` gate.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .autonomy.judgment import Judgment, JudgmentBackend, JudgmentKind, JudgmentQuestion
from .logging import get_logger

log = get_logger("tstd.cu_verify")

VerificationStatus = Literal["verified", "refuted", "unavailable"]

_VERIFICATION_INSTRUCTION = (
    "A computer-use action was executed. Judge whether it achieved its intended "
    "effect, using the action, the observed state before and after, and the "
    "action's own output. Answer yes only when the after-state or output shows "
    "the intended effect; answer no when it clearly did not."
)


@dataclass(frozen=True)
class Verification:
    """The outcome of one actuation verification."""

    status: VerificationStatus
    judgment: Judgment | None
    detail: str


class StateProbe(Protocol):
    """Captures a compact text snapshot of the surface a tool actuates on."""

    async def snapshot(self, tool_name: str) -> str | None:
        """State text, or ``None`` when the driver cannot describe it."""
        ...


class DriverStateProbe:
    """Best-effort state from the CU drivers (dev build).

    Reads only driver-exposed attributes — the mock drivers' scripted
    state today; richer AX/DOM summaries are the hardening path.  A
    driver that exposes nothing yields ``None``, and verification is
    simply unavailable for that call: today's behavior, by design.
    """

    def __init__(self, desktop: object, browser: object) -> None:
        self._desktop = desktop
        self._browser = browser

    async def snapshot(self, tool_name: str) -> str | None:
        driver = self._browser if tool_name.startswith("browser_") else self._desktop
        if driver is None:
            return None
        if tool_name.startswith("browser_"):
            return _browser_state(driver)
        return _desktop_state(driver)


def _browser_state(driver: object) -> str | None:
    url = getattr(driver, "url", None)
    if not isinstance(url, str):
        return None
    pages = getattr(driver, "pages", None)
    title = pages.get(url) if isinstance(pages, dict) else None
    return f"url={url} title={title if isinstance(title, str) else '?'}"


def _desktop_state(driver: object) -> str | None:
    title = getattr(driver, "foreground_title", None)
    app = getattr(driver, "foreground_app", None)
    if not isinstance(title, str) and not isinstance(app, str):
        return None
    app_text = app if isinstance(app, str) else "?"
    title_text = title if isinstance(title, str) else "?"
    return f"foreground_app={app_text} foreground_title={title_text}"


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


class ActuationVerifier:
    """Judges whether an actuating tool call had its intended effect.

    Args:
        backend: The judgment seam connector.
        threshold: Minimum confidence to assert verified/refuted; below
            it the call is ``unavailable`` — never a new block.
        max_state_chars: Per-field cap on the state sent to the backend.
    """

    def __init__(
        self,
        backend: JudgmentBackend,
        *,
        threshold: float = 0.6,
        max_state_chars: int = 2000,
    ) -> None:
        self._backend = backend
        self._threshold = threshold
        self._max_state_chars = max_state_chars

    async def verify(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        before: str | None,
        after: str | None,
        output: str,
    ) -> Verification:
        """Judge one executed actuation.  Never raises."""
        if before is None and after is None:
            return Verification("unavailable", None, "driver cannot describe state")
        cap = self._max_state_chars
        question = JudgmentQuestion(
            kind=JudgmentKind.NOUL,
            instructions=_VERIFICATION_INSTRUCTION,
            state=(
                ("Action", tool_name),
                ("Arguments", _cap(_canonical(arguments), cap)),
                ("State before", _cap(before or "(unknown)", cap)),
                ("State after", _cap(after or "(unknown)", cap)),
                ("Output", _cap(output, cap)),
            ),
            question_id="cu-verify",
        )
        started = time.perf_counter()
        try:
            judgment = await self._backend.judge(question)
        except Exception as exc:  # connectors should not raise; belt and braces
            log.warning(
                "actuation verification failed open",
                extra={"extra_fields": {"tool": tool_name, "error": str(exc)}},
            )
            return Verification("unavailable", None, "backend error")
        latency_ms = (time.perf_counter() - started) * 1000.0
        if not judgment.ok or judgment.confidence < self._threshold:
            return Verification("unavailable", judgment, "low confidence or parse failure")
        log.info(
            "actuation verification",
            extra={
                "extra_fields": {
                    "tool": tool_name,
                    "label": judgment.label,
                    "confidence": judgment.confidence,
                    "latency_ms": round(latency_ms, 1),
                    "backend": judgment.backend,
                }
            },
        )
        if judgment.label == "yes":
            return Verification("verified", judgment, "intended effect observed")
        return Verification("refuted", judgment, "intended effect not observed")


def _canonical(arguments: dict[str, Any]) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return json.dumps({k: str(v) for k, v in arguments.items()}, sort_keys=True)
