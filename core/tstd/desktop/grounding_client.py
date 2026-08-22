"""OpenAI-compatible vision client for click grounding (TD-3902).

Input is a screenshot plus a target phrase. Output is a point in
logical points, or a miss so the click path keeps the intended (x, y).

The destination is ``computer_use.grounding.base_url`` from config —
never a host literal here. Empty URL is off. A down or unresolved
loopback endpoint is a miss, not an exception. Cost is always zero:
the only legal destination is loopback. No live model is required
in CI; callers inject a double or a fake transport.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

from ..config import ModelDiscoveryError, is_loopback_url
from ..discovery import discover_model
from ..logging import get_logger

if TYPE_CHECKING:
    from ..config import GroundingConfig

log = get_logger("tstd.grounding")

_START_BOX = re.compile(
    r"start_box\s*=\s*['\"][([]?\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_POINT_TAG = re.compile(
    r"<point>\s*([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s*</point>",
    re.IGNORECASE,
)
_CLICK_PAIR = re.compile(
    r"click\s*\(\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedPoint:
    """A model-emitted point and the space it was written in."""

    x: float
    y: float
    space: Literal["points", "norm1000"]


@dataclass(frozen=True)
class GroundingResult:
    """One locate attempt. ``x``/``y`` are set only on a usable hit."""

    x: float | None
    y: float | None
    latency_ms: float
    cost: float
    source: Literal["model", "intended"]
    reason: str

    @classmethod
    def fallback(cls, *, reason: str, latency_ms: float = 0.0) -> GroundingResult:
        return cls(
            x=None,
            y=None,
            latency_ms=latency_ms,
            cost=0.0,
            source="intended",
            reason=reason,
        )

    @classmethod
    def hit(cls, x: float, y: float, *, latency_ms: float) -> GroundingResult:
        return cls(
            x=x,
            y=y,
            latency_ms=latency_ms,
            cost=0.0,
            source="model",
            reason="hit",
        )


class GroundingLocator(Protocol):
    """The seam the click path calls. Tests inject a double."""

    @property
    def enabled(self) -> bool: ...

    async def locate(
        self,
        screenshot_png: bytes,
        target: str,
        *,
        width_points: float | None = None,
        height_points: float | None = None,
    ) -> GroundingResult: ...


def parse_grounding_point(text: str) -> ParsedPoint | None:
    """Read a point from a vision-chat reply. None if nothing usable."""
    blob = _first_json_object(text)
    if blob is not None:
        xy = _xy_from_mapping(blob)
        if xy is not None:
            return ParsedPoint(xy[0], xy[1], "points")

    for pattern in (_START_BOX, _POINT_TAG, _CLICK_PAIR):
        match = pattern.search(text)
        if match is None:
            continue
        try:
            return ParsedPoint(float(match.group(1)), float(match.group(2)), "norm1000")
        except ValueError:
            return None
    return None


def as_logical_points(
    parsed: ParsedPoint,
    *,
    width_points: float | None,
    height_points: float | None,
) -> tuple[float, float] | None:
    """Map a parsed point into TD-3301 logical points. None if we cannot."""
    if parsed.space == "points":
        return (parsed.x, parsed.y)
    if width_points is None or height_points is None:
        return None
    if width_points <= 0 or height_points <= 0:
        return None
    return (parsed.x / 1000.0 * width_points, parsed.y / 1000.0 * height_points)


class GroundingClient:
    """POST ``{base_url}/chat/completions`` with a screenshot. Never raises."""

    def __init__(
        self,
        base_url: str,
        slug: str | None,
        timeout_seconds: float,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.slug = slug
        self.timeout_seconds = timeout_seconds
        self._http = client
        self._resolved_slug = slug

    @classmethod
    def from_config(cls, config: GroundingConfig) -> GroundingClient:
        return cls(config.base_url, config.slug, config.timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def locate(
        self,
        screenshot_png: bytes,
        target: str,
        *,
        width_points: float | None = None,
        height_points: float | None = None,
    ) -> GroundingResult:
        """Find *target* in *screenshot_png*, or miss so the click can fall back."""
        if not self.enabled:
            return GroundingResult.fallback(reason="off")
        if not is_loopback_url(self.base_url):
            return GroundingResult.fallback(reason="not_loopback")
        if not screenshot_png:
            return GroundingResult.fallback(reason="empty")

        started = time.perf_counter()
        try:
            point = await self._locate(
                screenshot_png,
                target,
                width_points=width_points,
                height_points=height_points,
            )
        except (httpx.HTTPError, ModelDiscoveryError, ValueError, KeyError, TypeError) as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            log.warning(
                "grounding unavailable; using intended click point",
                extra={"extra_fields": {"error": str(exc), "reason": _reason_for(exc)}},
            )
            return GroundingResult.fallback(reason=_reason_for(exc), latency_ms=latency_ms)

        latency_ms = (time.perf_counter() - started) * 1000.0
        if point is None:
            return GroundingResult.fallback(reason="unparseable", latency_ms=latency_ms)
        return GroundingResult.hit(point[0], point[1], latency_ms=latency_ms)

    async def _locate(
        self,
        screenshot_png: bytes,
        target: str,
        *,
        width_points: float | None,
        height_points: float | None,
    ) -> tuple[float, float] | None:
        slug = await self._resolve_slug()
        payload = await self._complete(slug, screenshot_png, target, width_points, height_points)
        text = _assistant_text(payload)
        if text is None:
            return None
        parsed = parse_grounding_point(text)
        if parsed is None:
            return None
        return as_logical_points(parsed, width_points=width_points, height_points=height_points)

    async def _resolve_slug(self) -> str:
        if self._resolved_slug:
            return self._resolved_slug
        resolved = await discover_model(self.base_url, client=self._http)
        self._resolved_slug = resolved
        return resolved

    async def _complete(
        self,
        slug: str,
        screenshot_png: bytes,
        target: str,
        width_points: float | None,
        height_points: float | None,
    ) -> Any:
        prompt = _locate_prompt(target, width_points, height_points)
        body = {
            "model": slug,
            "temperature": 0,
            "max_tokens": 128,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    "data:image/png;base64,"
                                    + base64.b64encode(screenshot_png).decode("ascii")
                                )
                            },
                        },
                    ],
                }
            ],
        }
        url = f"{self.base_url}/chat/completions"
        if self._http is not None:
            response = await self._http.post(url, json=body)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
            response = await http.post(url, json=body)
            response.raise_for_status()
            return response.json()


def _locate_prompt(target: str, width_points: float | None, height_points: float | None) -> str:
    phrase = target.strip() or "the primary clickable control"
    size = ""
    if width_points is not None and height_points is not None:
        size = (
            f" The screenshot is {width_points:g} by {height_points:g} logical points "
            "(origin top-left)."
        )
    return (
        "Locate this UI target on the screenshot and reply with JSON only: "
        '{"x": <float>, "y": <float>}. Coordinates are logical points.'
        f"{size} Target: {phrase}"
    )


def _first_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _xy_from_mapping(data: dict[str, Any]) -> tuple[float, float] | None:
    if "x" in data and "y" in data:
        try:
            return (float(data["x"]), float(data["y"]))
        except (TypeError, ValueError):
            return None
    point = data.get("point") or data.get("coordinate") or data.get("xy")
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        try:
            return (float(point[0]), float(point[1]))
        except (TypeError, ValueError):
            return None
    return None


def _assistant_text(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts) if parts else None
    return None


def _reason_for(exc: BaseException) -> str:
    if isinstance(exc, ModelDiscoveryError):
        return "unresolved"
    if isinstance(exc, httpx.HTTPError):
        return "down"
    return "unparseable"
