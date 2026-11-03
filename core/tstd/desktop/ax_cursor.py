"""agent-desktop's own per-agent cursor for the accessibility tools (TD-5227).

The ``desktop_*`` element tools act through agent-desktop without moving the
pointer. agent-desktop draws a presentation-only cursor for its actions, one
per agent when its session runs with ``--multi-agent``. TST Desk uses that
renderer for these tools and gives each session the same label and color as
its tst-cu-mcp cursor, so an agent looks the same whichever path it acts on.

How it is wired:

* One agent-desktop session per daemon run, started on the first command
  while the cursor is wanted (``session start --cursor --multi-agent
  --no-trace``). From then on every command runs in that session's
  namespace with ``--session`` and ``--agent-id``, cursor on or off, so a
  ref from a snapshot always resolves in the same namespace.
* Each session's agent is enabled once with its label and fill color.
* The cursor is wanted while "Agent cursor" and "Show on the real display"
  are both on and computer use is not stopped. Turning it off (Stop
  included) disables the overlay for the whole session, which removes every
  cursor; it comes back on the next command once wanted again.
* No session is ever started while the cursor is off: the tools then run
  exactly as before.

A failure here never fails a tool: the command runs without the cursor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..cu_agents import CuAgent, current_cu_agent, fallback_color
from ..logging import get_logger

log = get_logger("tstd.desktop.ax_cursor")

#: The agent id used when no session is acting (a tool called outside a loop).
DEFAULT_AGENT = CuAgent("desk")


class ScopedRunner(Protocol):
    async def run_with(self, prefix: list[str], command: str, *args: str) -> Any: ...


class AxCursor:
    """Starts, scopes and switches agent-desktop's multi-agent cursor."""

    def __init__(
        self,
        *,
        wanted: Callable[[], bool],
        color_for: Callable[[CuAgent], Awaitable[str | None]],
    ) -> None:
        self._wanted = wanted
        self._color_for = color_for
        self._session: str | None = None
        self._enabled: set[str] = set()
        self._overlay_on = False
        self._failed = False
        # Sessions act concurrently: two first commands at once must start
        # one agent-desktop session, not two (TD-5231). Only the set-up is
        # held, never the command itself.
        self._lock = asyncio.Lock()

    @property
    def session_id(self) -> str | None:
        return self._session

    async def scope(self, cli: ScopedRunner) -> list[str]:
        """Global flags for the next command; turns the cursor on or off first."""
        async with self._lock:
            return await self._scope(cli)

    async def _scope(self, cli: ScopedRunner) -> list[str]:
        agent = current_cu_agent.get() or DEFAULT_AGENT
        wanted = self._wanted()
        if self._session is None:
            if not wanted or self._failed:
                return []
            await self._start(cli)
            if self._session is None:
                return []
        prefix = ["--session", self._session, "--agent-id", agent.agent_id]
        if not wanted:
            await self._off(cli)
            return prefix
        try:
            if not self._overlay_on:
                await cli.run_with(
                    ["--session", self._session], "cursor-overlay", "enable", "--multi-agent"
                )
                self._overlay_on = True
                self._enabled.clear()
            if agent.agent_id not in self._enabled:
                color = await self._color_for(agent) or fallback_color(agent.agent_id)
                await cli.run_with(
                    prefix, "cursor-overlay", "enable", "--label", agent.label, "--fill", color
                )
                self._enabled.add(agent.agent_id)
        except Exception as exc:
            log.warning("agent-desktop cursor not enabled: %s", exc)
        return prefix

    async def off(self, cli: ScopedRunner) -> None:
        """Remove every agent's cursor (Stop, or a switch turned off)."""
        async with self._lock:
            await self._off(cli)

    async def _off(self, cli: ScopedRunner) -> None:
        if self._session is None or not self._overlay_on:
            return
        try:
            await cli.run_with(["--session", self._session], "cursor-overlay", "disable")
        except Exception as exc:
            log.warning("agent-desktop cursor not disabled: %s", exc)
        self._overlay_on = False
        self._enabled.clear()

    async def end(self, cli: ScopedRunner) -> None:
        """End the session at shutdown; its cursors go with it."""
        async with self._lock:
            await self._end(cli)

    async def _end(self, cli: ScopedRunner) -> None:
        session, self._session = self._session, None
        self._overlay_on = False
        self._enabled.clear()
        if session is None:
            return
        try:
            await cli.run_with([], "session", "end", session)
        except Exception as exc:
            log.warning("agent-desktop session not ended: %s", exc)

    async def _start(self, cli: ScopedRunner) -> None:
        try:
            data = await cli.run_with(
                [],
                "session",
                "start",
                "--cursor",
                "--multi-agent",
                "--no-trace",
                "--name",
                "tst-desk",
            )
        except Exception as exc:
            # An older or odd binary: run the tools without a cursor rather
            # than retrying a failing start on every command.
            self._failed = True
            log.warning("agent-desktop cursor session not started: %s", exc)
            return
        session = data.get("session_id") if isinstance(data, dict) else None
        if isinstance(session, str) and session:
            self._session = session
            self._overlay_on = True
        else:
            self._failed = True


__all__ = ["DEFAULT_AGENT", "AxCursor"]
