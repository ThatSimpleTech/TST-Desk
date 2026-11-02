"""A scripted ``agent-desktop`` for the TD-5220 tests.

Envelopes under ``fixtures/agent_desktop`` are real CLI output from a
GTK form, a GTK text editor and a Chromium login page, with paths and
titles made generic. The fake answers each command with one of them and
records what was asked, so a test can prove a command was never sent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from tstd.cu_policy import CuPolicy
from tstd.desktop.ax_cli import AgentDesktopCli
from tstd.desktop.ax_driver import AccessibilityDriver

FIXTURES = Path(__file__).parent / "fixtures" / "agent_desktop"
FORM_WINDOW = "w-1.300-org-a11y-atspi-accessible-6"
USERNAME = "@s3wspdr2z8h6x:e1"
PASSWORD = "@s3wspdr2z8h6x:e2"
REMEMBER = "@s3wspdr2z8h6x:e3"
SIGN_IN = "@s3wspdr2z8h6x:e4"


def fixture(name: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return loaded


class FakeCli:
    """Routes ``command`` (or ``get:<property>``, ``snapshot:skeleton``) to fixtures.

    A route that is a list is consumed in order, so a retry can see a
    different answer the second time.
    """

    def __init__(self, routes: Mapping[str, str | list[str]]) -> None:
        self.routes: dict[str, str | list[str]] = {
            k: list(v) if isinstance(v, list) else v for k, v in routes.items()
        }
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    async def __call__(
        self, argv: list[str], env: Mapping[str, str], limit_seconds: float
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(argv[1:])
        self.envs.append(dict(env))
        command = argv[1]
        key = command
        if command == "get":
            key = f"get:{argv[argv.index('--property') + 1]}"
        elif command == "snapshot" and "--skeleton" in argv:
            key = "snapshot:skeleton"
        route = self.routes[key]
        name = route.pop(0) if isinstance(route, list) else route
        body = (FIXTURES / name).read_bytes()
        return (0 if json.loads(body)["ok"] else 1), body, b""

    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]


def make_cli(tmp_path: Path, fake: FakeCli, **kwargs: object) -> AgentDesktopCli:
    binary = tmp_path / "agent-desktop"
    binary.write_bytes(b"pinned test binary")
    return AgentDesktopCli(
        binary,
        sha256=hashlib.sha256(b"pinned test binary").hexdigest(),
        state_dir=tmp_path / "state",
        runner=fake,
        base_env={"NO_AT_BRIDGE": "1", "PATH": "/usr/bin", "DISPLAY": ":1"},
        **kwargs,  # type: ignore[arg-type]
    )


def make_driver(
    tmp_path: Path,
    fake: FakeCli,
    *,
    policy: CuPolicy | None = None,
    max_snapshot_chars: int = 12000,
) -> AccessibilityDriver:
    chosen = policy if policy is not None else CuPolicy()
    return AccessibilityDriver(
        make_cli(tmp_path, fake),
        max_snapshot_chars=max_snapshot_chars,
        policy=lambda: chosen,
    )
