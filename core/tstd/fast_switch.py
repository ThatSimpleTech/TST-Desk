"""Calls a model's Fast switch (TD-5176).

A local model's ``fast.hook_url`` is a loopback switch that moves hardware
(a GPU lender: ``POST /fast {"model", "on", "by"}`` → 200 with the new
state, or 409 with ``error`` when it will not switch now, e.g. family chat is
mid-reply). The switch can also end Fast on its own (idle, a media job), so
the daemon asks it again at the start of a turn and the toggle follows.

An API model has no switch: its ``extra_body`` rides on requests while Fast
is on, and nothing here is called.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import FastConfig
from .logging import get_logger

log = get_logger("tstd.fast_switch")

#: The switch may wait for a busy service before answering (e.g. up to 60 s).
SWITCH_TIMEOUT_S = 150.0
STATUS_TIMEOUT_S = 5.0


class FastSwitchError(Exception):
    """The switch said no, or could not be reached. The message is for the person."""


def _model_state(body: Any, model: str | None) -> dict[str, Any]:
    """The one model's state from a switch reply: flat, or keyed by model."""
    if isinstance(body, dict) and model and isinstance(body.get(model), dict):
        return dict(body[model])
    return dict(body) if isinstance(body, dict) else {}


async def switch(cfg: FastConfig, on: bool, *, by: str = "tst-desk") -> dict[str, Any]:
    """Turn Fast on or off at the model's switch. Raises :class:`FastSwitchError`."""
    assert cfg.hook_url is not None
    payload = {"model": cfg.hook_model, "on": on, "by": by}
    try:
        async with httpx.AsyncClient(timeout=SWITCH_TIMEOUT_S) as client:
            resp = await client.post(cfg.hook_url, json=payload)
    except httpx.HTTPError as exc:
        raise FastSwitchError(f"The fast switch did not answer ({type(exc).__name__}).") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
        detail = body.get("error") if isinstance(body, dict) else None
        raise FastSwitchError(str(detail or f"The fast switch refused (HTTP {resp.status_code})."))
    return _model_state(body, cfg.hook_model)


async def is_on(cfg: FastConfig) -> tuple[bool, str] | None:
    """(on, last change) from the switch, or None when it cannot say."""
    assert cfg.hook_url is not None
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT_S) as client:
            resp = await client.get(cfg.hook_url)
        state = _model_state(resp.json(), cfg.hook_model)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "fast status unavailable", extra={"extra_fields": {"error": type(exc).__name__}}
        )
        return None
    if "on" not in state:
        return None
    return bool(state["on"]), str(state.get("last") or "")
