"""Element-level desktop driver on the ``agent-desktop`` CLI (TD-5220).

Reads a window's accessibility tree and acts on one element by ref,
without moving the pointer or taking focus. Beside the coordinate driver,
not instead of it: browsers, canvases, remote desktops and TST Desk's own
web view expose little or nothing here, and the coordinate tools stay.

Order for an action: kill-switch, argument checks, app policy, the
password guard, then the CLI. Reads are not gated by the kill-switch.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..cu_policy import CuPolicy, load_cu_policy
from . import ax_tree
from .ax_cli import AgentDesktopCli
from .protocol import DesktopError

if TYPE_CHECKING:
    from .ax_cursor import AxCursor

#: desktop_act action → CLI command, and whether it takes a value.
ACTIONS: dict[str, tuple[str, bool]] = {
    "click": ("click", False),
    "type": ("type", True),
    "set_value": ("set-value", True),
    "toggle": ("toggle", False),
    "select": ("select", True),
    "expand": ("expand", False),
    "scroll_to": ("scroll-to", False),
}
#: Actions that put text into a field: refused on a password field.
TEXT_ACTIONS = frozenset({"type", "set_value"})

#: Apps that need the renderer flag before their tree is readable.
CHROMIUM_FAMILY = ("chrome", "chromium", "brave", "msedge", "microsoft-edge", "electron")
#: Every launch: Chromium wants ACCESSIBILITY_ENABLED when the desktop's
#: accessibility switch is off; Qt wants QT_ACCESSIBILITY.
LAUNCH_ENV = ("ACCESSIBILITY_ENABLED=1", "QT_ACCESSIBILITY=1")

_REF = re.compile(r"^@(?P<sid>[A-Za-z0-9]+):e\d+$")
_SNAPSHOT_MEMORY = 32
_STATE_CHARS = 2000

SECURE_REFUSAL = (
    "That is a password field. TST Desk never types into password fields. "
    "Ask the user to enter it themselves, then carry on."
)


@dataclass
class _Snapshot:
    window_id: str
    title: str
    app: str
    nodes: dict[str, ax_tree.NodeInfo] = field(default_factory=dict)


def _snapshot_id(ref: str) -> str:
    match = _REF.match(ref.strip())
    if match is None:
        raise DesktopError(
            "ax_invalid_args",
            f"{ref!r} is not a ref. Use one exactly as desktop_snapshot printed it, "
            "for example @s8f3k2p9:e1.",
        )
    return match.group("sid")


def _needle_matches(needle: str, app: str) -> bool:
    folded = needle.strip().casefold()
    return bool(folded) and folded in app.casefold()


class AccessibilityDriver:
    """One CLI, shared across sessions so the kill-switch is process-wide."""

    def __init__(
        self,
        cli: AgentDesktopCli,
        *,
        max_snapshot_chars: int = 12000,
        policy: Callable[[], CuPolicy] = load_cu_policy,
    ) -> None:
        self._cli = cli
        self._max_chars = max_snapshot_chars
        self._policy = policy
        self._snapshots: OrderedDict[str, _Snapshot] = OrderedDict()
        self.killed = False

    def set_killed(self, killed: bool) -> None:
        self.killed = bool(killed)

    def attach_cursor(self, cursor: AxCursor) -> None:
        """Draw agent-desktop's per-agent cursor for these tools (TD-5227)."""
        self._cli.cursor = cursor

    async def cursor_off(self) -> None:
        """Remove every agent's cursor now (Stop, or a switch turned off)."""
        if self._cli.cursor is not None:
            await self._cli.cursor.off(self._cli)

    async def aclose(self) -> None:
        """End the cursor session, if one was started; commands leave nothing else."""
        if self._cli.cursor is not None:
            await self._cli.cursor.end(self._cli)

    # -- policy -----------------------------------------------------------

    async def _read_cu_policy(self) -> CuPolicy:
        return await asyncio.to_thread(self._policy)

    @staticmethod
    def _app_allowed(policy: CuPolicy, app: str) -> bool:
        if any(_needle_matches(item, app) for item in policy.denied_apps):
            return False
        if policy.allowed_apps:
            return any(_needle_matches(item, app) for item in policy.allowed_apps)
        return True

    async def _check_app(self, app: str) -> None:
        if not self._app_allowed(await self._read_cu_policy(), app):
            raise DesktopError(
                "ax_app_denied",
                f"{app!r} is blocked by the allowed/denied apps list in Settings → "
                "Computer use. Nothing was read or changed.",
            )

    async def _check_actuation(self) -> None:
        if self.killed:
            raise DesktopError("cu_killed", "computer-use kill-switch is engaged")
        if not (await self._read_cu_policy()).enabled:
            raise DesktopError(
                "cu_disabled", "Computer-use actions are turned off in Settings → Computer use."
            )

    # -- reads ------------------------------------------------------------

    async def windows(self, app: str | None = None) -> str:
        """Visible windows of allowed apps. Read-only."""
        args = ["--app", app] if app else []
        data = await self._cli.run("list-windows", *args)
        policy = await self._read_cu_policy()
        rows: list[dict[str, Any]] = []
        for win in data if isinstance(data, list) else []:
            if not isinstance(win, dict) or not win.get("visible", True):
                continue
            bounds = win.get("bounds")
            if (
                isinstance(bounds, dict)
                and min(bounds.get("width", 2), bounds.get("height", 2)) <= 1
            ):
                continue
            app_name = str(win.get("app_name") or "")
            if not self._app_allowed(policy, app_name):
                continue  # Omitted, not named.
            row: dict[str, Any] = {
                "window_id": win.get("id"),
                "app": app_name,
                "title": win.get("title") or "",
                "pid": win.get("pid"),
            }
            if win.get("is_focused"):
                row["focused"] = True
            if win.get("minimized"):
                row["minimized"] = True
            if win.get("accessible") is False:
                row["readable"] = False
            rows.append(row)
        return json.dumps({"windows": rows})

    async def snapshot(
        self,
        app: str | None = None,
        window_id: str | None = None,
        root: str | None = None,
    ) -> str:
        """Interactive elements of one window, filtered and size-capped. Read-only."""
        if root:
            parent = self._snapshots.get(_snapshot_id(root))
            if parent is None:
                raise DesktopError(
                    "ax_stale_ref", "That ref is not from a recent snapshot. Take a new one."
                )
            target = ["--root", root]
        elif window_id:
            target = ["--window-id", window_id]
        elif app:
            await self._check_app(app)
            target = ["--app", app]
        else:
            raise DesktopError(
                "ax_invalid_args", "Pass window_id (from desktop_windows), app, or root."
            )
        data = await self._read(target)
        text, cut = self._render(data)
        if cut and not root:
            data = await self._read([*target, "--skeleton"])
            text, _cut = self._render(data, skeleton=True)
        return text

    async def _read(self, target: list[str]) -> dict[str, Any]:
        raw = await self._cli.run("snapshot", "-i", "--compact", *target)
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        app = str(data.get("app") or "")
        await self._check_app(app)
        self._remember(data, target)
        return data

    def _remember(self, data: dict[str, Any], target: list[str]) -> None:
        sid = data.get("snapshot_id")
        tree = data.get("tree")
        if not isinstance(sid, str) or not isinstance(tree, dict):
            return
        window = data.get("window")
        win = window if isinstance(window, dict) else {}
        window_id = str(win.get("id") or "")
        title = str(win.get("title") or "")
        if not window_id and target[:1] == ["--root"]:
            parent = self._snapshots.get(_snapshot_id(target[1]))
            if parent is not None:
                window_id, title = parent.window_id, parent.title
        self._snapshots[sid] = _Snapshot(
            window_id=window_id,
            title=title,
            app=str(data.get("app") or ""),
            nodes=ax_tree.index(tree),
        )
        while len(self._snapshots) > _SNAPSHOT_MEMORY:
            self._snapshots.popitem(last=False)

    def _render(self, data: dict[str, Any], *, skeleton: bool = False) -> tuple[str, bool]:
        tree = data.get("tree")
        window = data.get("window")
        win = window if isinstance(window, dict) else {}
        header = (
            f"app {json.dumps(str(data.get('app') or ''))} "
            f"window {json.dumps(str(win.get('title') or ''), ensure_ascii=False)} "
            f"window_id {win.get('id') or '?'} snapshot {data.get('snapshot_id') or '?'}"
        )
        if skeleton:
            header += " (skeleton: the full tree was too large; open a branch with root=<ref>)"
        if data.get("complete") is False:
            header += " (incomplete: the app did not answer for every node)"
        pruned = ax_tree.prune(tree) if isinstance(tree, dict) else None
        lines = [header, *(ax_tree.render(pruned) if pruned is not None else [])]
        return ax_tree.cap(lines, self._max_chars)

    # -- actions ----------------------------------------------------------

    async def act(self, ref: str, action: str, value: str | None = None) -> str:
        """Act on one element by ref. Refusals raise before anything is sent."""
        await self._check_actuation()
        spec = ACTIONS.get(action)
        if spec is None:
            raise DesktopError(
                "ax_invalid_args", f"Unknown action {action!r}. Use one of: {', '.join(ACTIONS)}."
            )
        command, needs_value = spec
        if needs_value and value is None:
            raise DesktopError("ax_invalid_args", f"{action} needs a value.")
        snap = self._snapshots.get(_snapshot_id(ref))
        if snap is None:
            raise DesktopError(
                "ax_stale_ref",
                "That ref is not from a recent desktop_snapshot. Take a new one and use its refs.",
            )
        await self._check_app(snap.app)
        if action in TEXT_ACTIONS:
            await self._refuse_secure(ref, snap)
        # "--" so a value such as "-5" is text, not a flag.
        args = ["--", ref, value] if needs_value and value is not None else [ref]
        data = await self._cli.run(command, *args)
        return json.dumps(_act_result(action, ref, snap, data, redact=action in TEXT_ACTIONS))

    async def _refuse_secure(self, ref: str, snap: _Snapshot) -> None:
        """The password guard: the snapshot's view, then the live element."""
        cached = snap.nodes.get(ref)
        if cached is not None and cached.secure:
            raise DesktopError("ax_secure_field", SECURE_REFUSAL)
        states = await self._cli.run("get", ref, "--property", "states")
        role = await self._cli.run("get", ref, "--property", "role")
        live_states = _get_value(states)
        live_role = _get_value(role)
        if ax_tree.is_secure(
            str(live_role or ""),
            [str(s) for s in live_states] if isinstance(live_states, list) else [],
        ):
            raise DesktopError("ax_secure_field", SECURE_REFUSAL)

    async def launch(self, app: str) -> str:
        """Start an app with its accessibility switched on. Returns its window."""
        await self._check_actuation()
        name = app.strip()
        if not name or name.startswith("-"):
            raise DesktopError("ax_invalid_args", "Pass an application name or path.")
        await self._check_app(Path(name).name)
        args = [name, "--no-attach", "--activate"]
        for pair in LAUNCH_ENV:
            args += ["--env", pair]
        if any(part in Path(name).name.casefold() for part in CHROMIUM_FAMILY):
            args.append("--arg=--force-renderer-accessibility")
        data = await self._cli.run("launch", *args)
        body: dict[str, Any] = data if isinstance(data, dict) else {}
        win = body.get("window")
        window = win if isinstance(win, dict) else {}
        result: dict[str, Any] = {
            "launched": body.get("app") or name,
            "pid": body.get("pid"),
            "window_id": window.get("id"),
            "title": window.get("title"),
        }
        if not window:
            result["note"] = "No window yet. Call desktop_windows in a moment."
        return json.dumps(result)

    # -- verification (TD-709) -------------------------------------------

    async def state_for_ref(self, ref: str) -> str | None:
        """Compact text state of the window a ref belongs to, or ``None``."""
        try:
            snap = self._snapshots.get(_snapshot_id(ref))
        except DesktopError:
            return None
        if snap is None or not snap.window_id:
            return None
        try:
            raw = await self._cli.run("snapshot", "-i", "--compact", "--window-id", snap.window_id)
        except DesktopError:
            return None
        return ax_tree.state_text(raw, _STATE_CHARS) if isinstance(raw, dict) else None


def _get_value(data: Any) -> Any:
    return data.get("value") if isinstance(data, dict) else None


def _act_result(
    action: str, ref: str, snap: _Snapshot, data: Any, *, redact: bool
) -> dict[str, Any]:
    body: dict[str, Any] = data if isinstance(data, dict) else {}
    disposition = body.get("disposition")
    disp = disposition if isinstance(disposition, dict) else {}
    result: dict[str, Any] = {
        "action": action,
        "ref": ref,
        "window": snap.title,
        "delivery": disp.get("delivery", "unknown"),
    }
    post = body.get("post_state")
    if isinstance(post, dict):
        state = dict(post)
        value = state.get("value")
        if ax_tree.is_secure(str(state.get("role") or ""), state.get("states") or []):
            state.pop("value", None)
        elif redact and isinstance(value, str):
            state["value"] = f"[{len(value)} chars]"
        result["post_state"] = state
    surfaces = body.get("surfaces")
    if surfaces:
        result["surfaces"] = surfaces
    return result
