"""Desktop computer-use tools on the dispatcher (TD-3301).

No real display. The mock is the TD-102 path; a fake stdio MCP proves
the live driver talks JSON-RPC without binding a socket.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from tests.test_dispatch import attach_auto_approver
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClass, DecisionClassifier
from tstd.config import ComputerUseConfig, ModelConfig, Preset, TierConfig
from tstd.daemon import Daemon
from tstd.desktop import (
    LIVE_PLATFORMS,
    MCP_TOOLS,
    TINY_PNG,
    DesktopError,
    McpDesktopDriver,
    MockDesktopDriver,
    driver_for_command,
    window_matches,
)
from tstd.desktop.protocol import TINY_PNG_B64
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import ScreenFrame
from tstd.tools import ToolDispatcher, UnclassifiedToolCall, create_registry
from tstd.tools.boundary import PathGuard
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.results import ToolResult

_FAKE_MCP = f"""
import json, sys
PNG = {TINY_PNG_B64!r}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": mid,
            "result": {{
                "protocolVersion": "2024-11-05",
                "capabilities": {{}},
                "serverInfo": {{"name": "fake-cu", "version": "0"}},
            }},
        }}))
    elif method == "notifications/initialized":
        continue
    elif method == "tools/call":
        params = msg.get("params") or {{}}
        name = params.get("name")
        args = params.get("arguments") or {{}}
        expect = args.get("expect_window")
        if expect == "mismatch":
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": mid,
                "error": {{
                    "code": -32000,
                    "message": "expected the foreground window to match "
                    "'mismatch', but it is 'Other'.",
                }},
            }}))
        elif name == "screenshot":
            img = {{"type": "image", "data": PNG, "mimeType": "image/png"}}
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": mid,
                "result": {{"content": [img]}},
            }}))
        else:
            text = json.dumps({{"ok": True, "tool": name}})
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": mid,
                "result": {{"content": [{{"type": "text", "text": text}}]}},
            }}))
    sys.stdout.flush()
"""


class _Log:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def add(self, event: object) -> object:
        self.events.append(event)
        return event


class _Session:
    def __init__(self, persist: Path) -> None:
        self.id = "sess-desktop"
        self.persist_dir = persist
        self.event_log = _Log()


class SpyGuard(PathGuard):
    def __init__(self, boundary: Boundary) -> None:
        super().__init__(boundary)
        self.seen: list[str] = []

    def canonicalize(self, raw: str | Path) -> Path:
        self.seen.append(f"canon:{raw}")
        return super().canonicalize(raw)

    def check_read(self, raw: str | Path) -> Path:
        self.seen.append(f"read:{raw}")
        return super().check_read(raw)

    def check_write(self, raw: str | Path) -> Path:
        self.seen.append(f"write:{raw}")
        return super().check_write(raw)


def _dispatcher(
    workspace: Path,
    driver: MockDesktopDriver,
    *,
    approve: bool = True,
    worker_log: list[str] | None = None,
) -> tuple[ToolDispatcher, SpyGuard]:
    calls = worker_log if worker_log is not None else []

    async def _worker(prompt: str) -> str:
        calls.append(prompt)
        return "B"

    guard = SpyGuard(Boundary(workspace_root=workspace))
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=workspace)),
            call_worker=_worker,
        ),
        path_guard=guard,
        policy=PolicyConfig(),
        workspace=workspace,
    )
    register_builtin_handlers(dispatcher, desktop_driver=driver)
    if approve:
        attach_auto_approver(dispatcher)
    return dispatcher, guard


class TestScreenFrame:
    async def test_screenshot_emits_screen_frame_path(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        dispatcher, _ = _dispatcher(tmp_path, driver)
        session = _Session(tmp_path / "sess")
        session.persist_dir.mkdir(parents=True, exist_ok=True)
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {}, session=session)
        assert result.status == "success"
        body = json.loads(result.output)
        assert body["png_base64"]
        frames = [e for e in session.event_log.events if isinstance(e, ScreenFrame)]
        assert len(frames) == 1
        assert frames[0].path.startswith("screens/")
        assert frames[0].path.endswith(".png")
        assert (session.persist_dir / frames[0].path).read_bytes().startswith(b"\x89PNG")
        sidecar = session.persist_dir / frames[0].path.replace(".png", ".dataurl")
        assert sidecar.read_text(encoding="utf-8").startswith("data:image/png;base64,")
        dumped = json.loads(frames[0].model_dump_json())
        assert "png_base64" not in dumped
        assert "content" not in dumped

    async def test_failed_click_is_tool_result_not_screen_frame(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver(foreground_title="Terminal", foreground_app="zsh")
        dispatcher, _ = _dispatcher(tmp_path, driver)
        session = _Session(tmp_path / "sess")
        session.persist_dir.mkdir(parents=True, exist_ok=True)
        result = await dispatcher.dispatch(
            "c1",
            "desktop_click",
            {"x": 4, "y": 5, "expect_window": "Chrome"},
            session=session,
        )
        assert isinstance(result, ToolResult)
        assert result.status == "error"
        assert result.error_code == "focus_mismatch"
        assert result.name == "desktop_click"
        assert driver.actuations == []
        assert session.event_log.events == []


class TestScreenshotClassA:
    async def test_screenshot_is_a_without_approval(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        worker: list[str] = []
        dispatcher, _ = _dispatcher(tmp_path, driver, approve=False, worker_log=worker)
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {})
        assert result.status == "success"
        assert result.decision_class is DecisionClass.A
        assert worker == []
        body = json.loads(result.output)
        assert body["png_base64"]
        raw = base64.b64decode(body["png_base64"])
        assert raw.startswith(b"\x89PNG")
        assert driver.actuations == []


class TestClickClassB:
    async def test_click_is_b_and_needs_approval(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        worker: list[str] = []
        dispatcher, _ = _dispatcher(tmp_path, driver, approve=False, worker_log=worker)
        with pytest.raises(UnclassifiedToolCall):
            await dispatcher.dispatch("c1", "desktop_click", {"x": 10, "y": 20})
        assert driver.actuations == []
        assert worker == []

    async def test_approved_click_runs(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        dispatcher, _ = _dispatcher(tmp_path, driver, approve=True)
        result = await dispatcher.dispatch("c1", "desktop_click", {"x": 10, "y": 20})
        assert result.status == "success"
        assert result.decision_class is DecisionClass.B
        assert driver.actuations == ["click"]

    async def test_denied_click_does_not_actuate(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        dispatcher, _ = _dispatcher(tmp_path, driver, approve=True)

        async def _deny(*_args: object) -> ApprovalOutcome:
            return ApprovalOutcome(False, "denied by user")

        dispatcher.approval_handler = _deny
        result = await dispatcher.dispatch("c1", "desktop_click", {"x": 1, "y": 2})
        assert result.status == "error"
        assert result.error_code == "approval_denied"
        assert driver.actuations == []


class TestFocusGuard:
    async def test_mismatch_refuses_without_actuating(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver(foreground_title="Terminal", foreground_app="zsh")
        dispatcher, _ = _dispatcher(tmp_path, driver)
        result = await dispatcher.dispatch(
            "c1",
            "desktop_click",
            {"x": 4, "y": 5, "expect_window": "Chrome"},
        )
        assert result.status == "error"
        assert result.error_code == "focus_mismatch"
        assert driver.actuations == []
        assert driver.calls == []

    def test_window_matches_title_or_app(self) -> None:
        assert window_matches("chrome", "Docs - Google Chrome", "Google Chrome")
        assert window_matches("Chrome", "x", "Google Chrome")
        assert not window_matches("Chrome", "Terminal", "zsh")
        assert not window_matches("  ", "Chrome", "chrome")


class TestKillSwitch:
    async def test_blocks_click_not_screenshot(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        dispatcher, _ = _dispatcher(tmp_path, driver)
        driver.set_killed(True)
        click = await dispatcher.dispatch("c1", "desktop_click", {"x": 1, "y": 2})
        shot = await dispatcher.dispatch("c2", "desktop_screenshot", {})
        assert click.status == "error"
        assert click.error_code == "cu_killed"
        assert driver.actuations == []
        assert shot.status == "success"
        assert shot.decision_class is DecisionClass.A
        assert [name for name, _ in driver.calls] == ["screenshot"]

    def test_daemon_api_delegates(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path)
        mock = MockDesktopDriver()
        daemon.desktop_driver = mock
        daemon.set_computer_use_killed(True)
        assert mock.killed is True
        daemon.set_computer_use_killed(False)
        assert mock.killed is False


class TestNoPathGuard:
    async def test_desktop_tools_never_touch_path_guard(self, tmp_path: Path) -> None:
        driver = MockDesktopDriver()
        dispatcher, guard = _dispatcher(tmp_path, driver)
        shot = await dispatcher.dispatch("c1", "desktop_screenshot", {})
        click = await dispatcher.dispatch("c2", "desktop_click", {"x": 1, "y": 2})
        typed = await dispatcher.dispatch("c3", "desktop_type", {"text": "hi"})
        assert shot.status == "success"
        assert click.status == "success"
        assert typed.status == "success"
        assert guard.seen == []


class TestLinuxAndLivePath:
    def test_empty_command_is_mock(self) -> None:
        assert isinstance(driver_for_command(""), MockDesktopDriver)
        assert isinstance(driver_for_command([]), MockDesktopDriver)

    def test_command_is_mcp_live_path(self) -> None:
        driver = driver_for_command("python -m tst_cu_mcp")
        assert isinstance(driver, McpDesktopDriver)
        assert "darwin" in LIVE_PLATFORMS and "win32" in LIVE_PLATFORMS
        assert "linux" in LIVE_PLATFORMS
        assert len(LIVE_PLATFORMS) == 3
        assert MCP_TOOLS["move"] == "move_mouse"
        assert MCP_TOOLS["type"] == "type_text"

    async def test_unknown_os_live_actuation_is_e20(self) -> None:
        driver = McpDesktopDriver(["/bin/false"], platform="freebsd")
        with pytest.raises(DesktopError) as exc:
            await driver.click(1, 2)
        assert exc.value.code == "e20"
        # Mock still works on linux — factory does not look at the OS.
        mock = MockDesktopDriver()
        out = await mock.click(1, 2)
        assert json.loads(out)["clicked"]["x"] == 1

    async def test_unknown_os_live_screenshot_is_e20(self, tmp_path: Path) -> None:
        driver = McpDesktopDriver(["/bin/false"], platform="freebsd")
        dispatcher, _ = _dispatcher(tmp_path, MockDesktopDriver())
        # Swap in the live driver after wiring so we exercise the handler.
        register_builtin_handlers(dispatcher, desktop_driver=driver)
        result = await dispatcher.dispatch("c1", "desktop_screenshot", {})
        assert result.status == "error"
        assert result.error_code == "e20"

    @pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
    async def test_live_path_speaks_stdio_mcp(self, tmp_path: Path, platform: str) -> None:
        script = tmp_path / "fake_cu_mcp.py"
        script.write_text(_FAKE_MCP, encoding="utf-8")
        driver = McpDesktopDriver(
            [sys.executable, str(script)],
            platform=platform,
        )
        try:
            shot = json.loads(await driver.screenshot())
            assert shot["png_base64"] == TINY_PNG_B64
            clicked = json.loads(await driver.click(3, 4))
            assert clicked["clicked"] == {"x": 3, "y": 4}
            with pytest.raises(DesktopError) as exc:
                await driver.click(3, 4, expect_window="mismatch")
            assert exc.value.code == "focus_mismatch"
        finally:
            await driver.aclose()

    def test_desktop_package_does_not_bind(self) -> None:
        root = Path(__file__).resolve().parents[1] / "tstd" / "desktop"
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "0.0.0.0" not in text
            assert "bind(" not in text


class TestMockNeverMovesAPointer:
    async def test_tiny_png_is_real_png(self) -> None:
        assert TINY_PNG.startswith(b"\x89PNG")
        driver = MockDesktopDriver()
        await driver.move(9, 8)
        await driver.scroll(0, -3)
        assert driver.actuations == ["move", "scroll"]


def _config_with_cu_command(command: str) -> ModelConfig:
    def tier(slug: str) -> TierConfig:
        return TierConfig(
            slug=slug,
            base_url="http://mock.local/v1",
            input_price=1.0,
            output_price=2.0,
            cache_read_price=0.5,
            context_window=100_000,
            max_output_tokens=1_000,
        )

    return ModelConfig(
        presets={
            "test": Preset(
                brain=tier("test-brain"),
                worker=tier("test-worker"),
                validator=tier("test-validator"),
            ),
        },
        active_preset="test",
        computer_use=ComputerUseConfig(command=command),
    )


class TestOverlayEnvPlumbing:
    """The daemon's show_on_real_display pref reaches the sidecar as env."""

    def test_prefs_on_threads_overlay_one(self) -> None:
        from tstd.cu_indicators import CuIndicatorPrefs
        from tstd.desktop.factory import desktop_driver_from_config

        driver = desktop_driver_from_config(
            _config_with_cu_command("python -m tst_cu_mcp"),
            CuIndicatorPrefs(show_on_real_display=True),
        )
        assert isinstance(driver, McpDesktopDriver)
        assert driver._client._env is not None
        assert driver._client._env["TST_CU_MCP_OVERLAY"] == "1"
        # The rest of the parent environment still reaches the child.
        assert "PATH" in driver._client._env

    def test_prefs_off_threads_overlay_zero(self) -> None:
        from tstd.cu_indicators import CuIndicatorPrefs
        from tstd.desktop.factory import desktop_driver_from_config

        driver = desktop_driver_from_config(
            _config_with_cu_command("python -m tst_cu_mcp"),
            CuIndicatorPrefs(show_on_real_display=False),
        )
        assert isinstance(driver, McpDesktopDriver)
        assert driver._client._env is not None
        assert driver._client._env["TST_CU_MCP_OVERLAY"] == "0"

    def test_without_prefs_no_env_override(self) -> None:
        from tstd.desktop.factory import desktop_driver_from_config

        driver = desktop_driver_from_config(_config_with_cu_command("python -m tst_cu_mcp"))
        assert isinstance(driver, McpDesktopDriver)
        assert driver._client._env is None

    def test_empty_command_is_mock_even_with_prefs(self) -> None:
        from tstd.cu_indicators import CuIndicatorPrefs
        from tstd.desktop.factory import desktop_driver_from_config

        driver = desktop_driver_from_config(
            _config_with_cu_command(""), CuIndicatorPrefs(show_on_real_display=True)
        )
        assert isinstance(driver, MockDesktopDriver)

    async def test_sidecar_child_receives_the_env(self, tmp_path: Path) -> None:
        """End to end: a spawned sidecar actually sees the merged env."""
        from tstd.desktop.stdio_mcp import StdioMcpClient

        script = tmp_path / "env_probe_mcp.py"
        script.write_text(
            "import json, os, sys\n"
            "for line in sys.stdin:\n"
            "    line = line.strip()\n"
            "    if not line:\n"
            "        continue\n"
            "    msg = json.loads(line)\n"
            "    method = msg.get('method')\n"
            "    mid = msg.get('id')\n"
            "    if method == 'initialize':\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': {\n"
            "            'protocolVersion': '2024-11-05', 'capabilities': {},\n"
            "            'serverInfo': {'name': 'env-probe', 'version': '0'}}}))\n"
            "    elif method == 'tools/call':\n"
            "        seen = os.environ.get('TST_CU_MCP_OVERLAY', 'unset')\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': {\n"
            "            'content': [{'type': 'text', 'text': seen}]}}))\n"
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )
        client = StdioMcpClient([sys.executable, str(script)], env={"TST_CU_MCP_OVERLAY": "1"})
        try:
            result = await client.call_tool("probe")
            text = result["content"][0]["text"]
            assert text == "1"
        finally:
            await client.aclose()
