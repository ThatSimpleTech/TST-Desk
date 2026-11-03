"""Each session is its own computer-use agent with its own cursor (TD-5227).

* A session's loop runs as that session's agent (a context variable).
* The sidecar is told which agent acts before each pointer action, under
  one lock, and its color comes back for agent-desktop.
* agent-desktop runs in one cursor session per daemon run, each agent
  enabled once with the same label and color; Stop and the switches turn
  every cursor off; shutdown ends the session.
* The display switches are mirrored into the shared policy file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tstd.cu_agents import CuAgent, current_cu_agent, fallback_color, set_title_lookup
from tstd.cu_policy import CuPolicy, save_cu_policy, save_overlay_prefs
from tstd.daemon import Daemon
from tstd.desktop.ax_cli import AgentDesktopCli
from tstd.desktop.ax_cursor import AxCursor
from tstd.desktop.mcp_driver import McpDesktopDriver
from tstd.session import Session, SessionRunner


@pytest.fixture(autouse=True)
def _no_titles() -> Any:
    yield
    set_title_lookup(lambda _sid: None)


class TestAgentIdentity:
    def test_id_is_short_and_safe_for_agent_desktop(self) -> None:
        agent = CuAgent("3f2a9c1e-77b0-4d1a-9e0f-123456789abc")
        assert agent.agent_id == "tst-3f2a9c1e77b0"

    def test_label_names_the_chat(self) -> None:
        set_title_lookup(lambda sid: "Fix the login form" if sid == "s1" else None)
        assert CuAgent("s1").label == "TST Desk · Fix the login form"
        assert CuAgent("s2").label == "TST Desk"

    def test_a_long_title_is_cut_to_forty(self) -> None:
        set_title_lookup(lambda _sid: "word " * 30)
        label = CuAgent("s1").label
        assert len(label) == 40
        assert label.endswith("…")

    def test_fallback_color_is_stable(self) -> None:
        assert fallback_color("tst-abc") == fallback_color("tst-abc")


async def test_a_sessions_loop_acts_as_that_session() -> None:
    session = Session("/tmp/ws")
    seen: list[CuAgent | None] = []

    async def loop(sess: Session) -> None:
        async def tool() -> None:
            seen.append(current_cu_agent.get())

        await asyncio.create_task(tool())

    runner = SessionRunner(session, loop_factory=loop)
    await runner.start()
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [CuAgent(session.id)]
    assert current_cu_agent.get() is None


class FakeClient:
    """Records sidecar calls; ``overlay_agent`` answers with a color."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append((name, dict(arguments or {})))
        if name == "overlay_agent":
            if self.fail:
                raise RuntimeError("sidecar gone")
            body = {"agent_id": arguments["agent_id"], "color": "#16A34A"}  # type: ignore[index]
            return {"content": [{"type": "text", "text": json.dumps(body)}]}
        return {"content": [{"type": "text", "text": "{}"}]}

    async def aclose(self) -> None:
        return None


def _driver(client: FakeClient) -> McpDesktopDriver:
    return McpDesktopDriver(["tst-cu-mcp"], platform="darwin", client=client)  # type: ignore[arg-type]


class TestSidecarAnnounce:
    async def test_pointer_actions_name_the_agent_once_per_change(self) -> None:
        client = FakeClient()
        driver = _driver(client)
        a, b = CuAgent("aaaa"), CuAgent("bbbb")
        token = current_cu_agent.set(a)
        await driver.click(1, 1)
        await driver.move(2, 2)
        current_cu_agent.set(b)
        await driver.scroll(0, -1, x=3, y=3)
        current_cu_agent.reset(token)
        names = [(name, args.get("agent_id")) for name, args in client.calls]
        assert names == [
            ("overlay_agent", "tst-aaaa"),
            ("click", None),
            ("move_mouse", None),
            ("overlay_agent", "tst-bbbb"),
            ("scroll", None),
        ]

    async def test_no_session_means_no_announcement(self) -> None:
        client = FakeClient()
        await _driver(client).click(1, 1)
        assert [name for name, _ in client.calls] == ["click"]

    async def test_the_color_comes_back_for_agent_desktop(self) -> None:
        driver = _driver(FakeClient())
        assert await driver.agent_color(CuAgent("aaaa")) == "#16A34A"

    async def test_a_failed_announcement_still_acts(self) -> None:
        client = FakeClient(fail=True)
        driver = _driver(client)
        token = current_cu_agent.set(CuAgent("aaaa"))
        try:
            await driver.click(1, 1)
        finally:
            current_cu_agent.reset(token)
        assert [name for name, _ in client.calls] == ["overlay_agent", "click"]
        assert await driver.agent_color(CuAgent("aaaa")) is None


class FakeCli:
    """A ScopedRunner that records argv and answers like agent-desktop."""

    def __init__(self, *, start_fails: bool = False) -> None:
        self.argv: list[list[str]] = []
        self.start_fails = start_fails

    async def run_with(self, prefix: list[str], command: str, *args: str) -> Any:
        self.argv.append([*prefix, command, *args])
        if command == "session" and args[:1] == ("start",):
            if self.start_fails:
                raise RuntimeError("unknown subcommand")
            return {"session_id": "run-1"}
        return {}


class TestAgentDesktopCursor:
    def _cursor(self, wanted: list[bool]) -> AxCursor:
        async def color_for(agent: CuAgent) -> str | None:
            return "#8B5CF6" if agent.agent_id == "tst-aaaa" else None

        return AxCursor(wanted=lambda: wanted[0], color_for=color_for)

    async def test_first_command_starts_a_cursor_session_and_enables_the_agent(self) -> None:
        cli, cursor = FakeCli(), self._cursor([True])
        token = current_cu_agent.set(CuAgent("aaaa"))
        try:
            first = await cursor.scope(cli)
            again = await cursor.scope(cli)
        finally:
            current_cu_agent.reset(token)
        assert first == again == ["--session", "run-1", "--agent-id", "tst-aaaa"]
        assert cli.argv == [
            ["session", "start", "--cursor", "--multi-agent", "--no-trace", "--name", "tst-desk"],
            [
                "--session",
                "run-1",
                "--agent-id",
                "tst-aaaa",
                "cursor-overlay",
                "enable",
                "--label",
                "TST Desk",
                "--fill",
                "#8B5CF6",
            ],
        ]

    async def test_a_second_agent_gets_its_own_cursor(self) -> None:
        cli, cursor = FakeCli(), self._cursor([True])
        for sid in ("aaaa", "bbbb"):
            token = current_cu_agent.set(CuAgent(sid))
            await cursor.scope(cli)
            current_cu_agent.reset(token)
        enables = [argv for argv in cli.argv if "enable" in argv]
        assert [argv[3] for argv in enables] == ["tst-aaaa", "tst-bbbb"]
        # No color from the sidecar: the stable fallback.
        assert enables[1][-1] == fallback_color("tst-bbbb")

    async def test_cursor_off_until_wanted_starts_nothing(self) -> None:
        cli, cursor = FakeCli(), self._cursor([False])
        assert await cursor.scope(cli) == []
        assert cli.argv == []

    async def test_turning_off_removes_every_cursor_and_keeps_the_namespace(self) -> None:
        wanted = [True]
        cli, cursor = FakeCli(), self._cursor(wanted)
        await cursor.scope(cli)
        wanted[0] = False
        prefix = await cursor.scope(cli)
        await cursor.scope(cli)
        assert prefix[:2] == ["--session", "run-1"]
        disables = [argv for argv in cli.argv if "disable" in argv]
        assert disables == [["--session", "run-1", "cursor-overlay", "disable"]]
        wanted[0] = True
        await cursor.scope(cli)
        assert cli.argv[-2] == [
            "--session",
            "run-1",
            "cursor-overlay",
            "enable",
            "--multi-agent",
        ]

    async def test_stop_turns_cursors_off_now_and_shutdown_ends_the_session(self) -> None:
        cli, cursor = FakeCli(), self._cursor([True])
        await cursor.scope(cli)
        await cursor.off(cli)
        await cursor.off(cli)
        await cursor.end(cli)
        assert cli.argv[-2:] == [
            ["--session", "run-1", "cursor-overlay", "disable"],
            ["session", "end", "run-1"],
        ]

    async def test_two_first_commands_at_once_start_one_session(self) -> None:
        """TD-5231: two sessions acting at once must share one namespace, or
        a ref from one session's snapshot is looked up in another."""

        class SlowStart(FakeCli):
            async def run_with(self, prefix: list[str], command: str, *args: str) -> Any:
                if command == "session":
                    await asyncio.sleep(0.05)
                return await super().run_with(prefix, command, *args)

        cli, cursor = SlowStart(), self._cursor([True])

        async def first(sid: str) -> list[str]:
            token = current_cu_agent.set(CuAgent(sid))
            try:
                return await cursor.scope(cli)
            finally:
                current_cu_agent.reset(token)

        a, b = await asyncio.gather(first("aaaa"), first("bbbb"))
        starts = [argv for argv in cli.argv if argv[:2] == ["session", "start"]]
        assert len(starts) == 1
        assert a[:2] == b[:2] == ["--session", "run-1"]

    async def test_a_failed_start_runs_without_a_cursor_and_does_not_retry(self) -> None:
        cli, cursor = FakeCli(start_fails=True), self._cursor([True])
        assert await cursor.scope(cli) == []
        assert await cursor.scope(cli) == []
        assert len(cli.argv) == 1

    async def test_the_cli_puts_the_scope_before_the_command(self, tmp_path: Path) -> None:
        binary = tmp_path / "agent-desktop"
        binary.write_bytes(b"x")
        seen: list[list[str]] = []

        async def runner(argv: list[str], env: Any, limit: float) -> tuple[int, bytes, bytes]:
            seen.append(argv)
            if argv[1:3] == ["session", "start"]:
                return 0, b'{"ok": true, "data": {"session_id": "run-9"}}', b""
            return 0, b'{"ok": true, "data": {}}', b""

        cli = AgentDesktopCli(binary, sha256="", runner=runner, packaged=True)
        cli.cursor = self._cursor([True])
        await cli.run("list-windows", "--app", "Finder")
        assert seen[-1][1:] == [
            "--session",
            "run-9",
            "--agent-id",
            "tst-desk",
            "list-windows",
            "--app",
            "Finder",
        ]


class TestSharedSwitches:
    def test_overlay_prefs_keep_every_other_key(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        save_cu_policy(CuPolicy(mode="full_control", denied_apps=("Mail",)), path)
        assert save_overlay_prefs(enabled=True, cursor=False, path=path) is True
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert body["overlay"] == {"enabled": True, "cursor": False}
        assert body["mode"] == "full_control"
        assert body["scoping"]["denied_apps"] == ["Mail"]
        # Unchanged switches do not rewrite the file.
        assert save_overlay_prefs(enabled=True, cursor=False, path=path) is False
        # Settings → Computer use keeps the cursor switch.
        save_cu_policy(CuPolicy(mode="background"), path)
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["overlay"]["cursor"] is False

    def test_an_unreadable_file_is_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("- not a mapping\n", encoding="utf-8")
        assert save_overlay_prefs(enabled=False, cursor=False, path=path) is False
        assert path.read_text(encoding="utf-8") == "- not a mapping\n"

    async def test_settings_mirror_the_switches_and_stop_clears_agent_desktop(
        self, tmp_path: Path
    ) -> None:
        daemon = Daemon(data_dir=tmp_path)

        class Ax:
            offs = 0

            async def cursor_off(self) -> None:
                Ax.offs += 1

            def set_killed(self, killed: bool) -> None:
                return None

            async def aclose(self) -> None:
                return None

        daemon.ax_driver = Ax()  # type: ignore[assignment]
        raw = await daemon._handle_message(
            json.dumps(
                {
                    "type": "set_cu_indicators",
                    "glow": True,
                    "agent_cursor": False,
                    "show_on_real_display": True,
                }
            ),
            None,
        )
        assert raw is not None
        shared = yaml.safe_load((Path.home() / ".tst-cu-mcp" / "config.yaml").read_text())
        assert shared["overlay"] == {"enabled": True, "cursor": False}
        assert Ax.offs == 1
        await daemon._handle_message(json.dumps({"type": "set_cu_kill", "killed": True}), None)
        assert Ax.offs == 2
        await daemon._shutdown()
