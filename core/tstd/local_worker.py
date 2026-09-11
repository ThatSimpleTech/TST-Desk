"""CU-heavy worker remap (TD-3903).

A session that has already used a ``desktop_`` or ``browser_`` tool — the
same signal as the Screen tab — may pin the *worker client* to another
preset's worker tier. Brain, lead-turns, ``set_tier``, and escalation stay
on the active preset (TD-303). Empty ``computer_use.local_worker_preset``
disables the remap. A name that is not a preset is treated the same as
empty so a test config without ``vllm`` does not crash.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from .config import ModelConfig, TierConfig, apply_credential_host
from .protocol import ToolCall
from .router import TIER_NAMES, TierName
from .session import Session

#: Grok reports MCP tools as ``<server>__<tool>``; the computer-use server
#: is registered as ``computer-use`` (``grok_home.computer_use_mcp``).
GROK_CU_PREFIX = "computer-use__"
#: Diagnostics that neither look at nor touch the desktop.
_GROK_CU_META = frozenset({"check_permissions", "health", "overlay_session"})

CuSurface = Literal["desktop", "browser"]


def is_cu_tool(name: str) -> bool:
    """True for the tools that make the Screen tab appear.

    Native names are ``desktop_*`` / ``browser_*``; a Grok session reports
    the same MCP tools as ``computer-use__*`` (its diagnostics excluded).
    """
    if name.startswith("desktop_") or name.startswith("browser_"):
        return True
    if name.startswith(GROK_CU_PREFIX):
        return name[len(GROK_CU_PREFIX) :] not in _GROK_CU_META
    return False


def session_is_cu_heavy(session: Session) -> bool:
    """True once this session has emitted a computer-use ``tool_call``."""
    if session.used_cu:
        return True
    for event in session.event_log.all_events:
        if isinstance(event, ToolCall) and is_cu_tool(event.name):
            session.used_cu = True
            return True
    return False


def mark_cu_tool(session: Session, name: str) -> None:
    """Record a computer-use tool the moment the Screen tab would see it."""
    if is_cu_tool(name):
        session.used_cu = True


def last_cu_surface(session: Session) -> CuSurface | None:
    """The last computer-use tool's surface, or ``None`` before any CU call.

    Design-mode hit-test routes from this: a desktop frame must not be
    asked of the browser driver (TD-3406).
    """
    for event in reversed(session.event_log.all_events):
        if isinstance(event, ToolCall) and is_cu_tool(event.name):
            if event.name.startswith("browser_"):
                return "browser"
            return "desktop"
    return None


def local_worker_tier(config: ModelConfig) -> TierConfig | None:
    """The named preset's worker tier, or ``None`` when remap is off."""
    name = config.computer_use.local_worker_preset.strip()
    if not name:
        return None
    preset = config.presets.get(name)
    if preset is None:
        return None
    return preset.worker


def effective_tier(config: ModelConfig, tier: TierName, *, cu_heavy: bool) -> TierConfig:
    """Tier used for the provider client and slug on this call.

    Only ``worker`` remaps, and only after the session is CU-heavy.
    """
    if tier == "worker" and cu_heavy:
        remapped = local_worker_tier(config)
        if remapped is not None:
            return apply_credential_host(config, remapped)
    return apply_credential_host(config, config.tier(tier))


def titlebar_slugs(config: ModelConfig, *, cu_heavy: bool) -> dict[str, str]:
    """Slugs for ``tier_state``. Unresolved remapped worker is omitted."""
    slugs: dict[str, str] = {}
    for name in TIER_NAMES:
        cfg = effective_tier(config, name, cu_heavy=cu_heavy)
        if cfg.slug is not None:
            slugs[name] = cfg.slug
    return slugs


def display_host(base_url: str) -> str:
    """Hostname:port for the title bar. Empty when the URL has no host."""
    parsed = urlparse(base_url)
    hostname = parsed.hostname
    if not hostname:
        return ""
    if parsed.port is None:
        return hostname
    return f"{hostname}:{parsed.port}"


def titlebar_hosts(config: ModelConfig, *, cu_heavy: bool) -> dict[str, str]:
    """Hosts for ``tier_state`` (TD-1720). Same URL the provider client calls."""
    hosts: dict[str, str] = {}
    for name in TIER_NAMES:
        cfg = effective_tier(config, name, cu_heavy=cu_heavy)
        host = display_host(cfg.base_url)
        if host:
            hosts[name] = host
    return hosts
