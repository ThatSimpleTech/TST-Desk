"""Tests for ``tst run`` (TD-3101).

The CLI is a protocol client of the same daemon the window uses. The
headless harness (TD-1401) stays the mock path — this file does not
call or reshape ``e2e_harness.run``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tstd import cli
from tstd.daemon import Daemon
from tstd.e2e_harness import run as harness_run
from tstd.mock import MockProvider, Script
from tstd.protocol import PROTOCOL_VERSION
from tstd.ws import write_port_file


def _workspace(root: Path) -> Path:
    path = root / "workspace"
    path.mkdir()
    return path


async def _start_daemon(data_dir: Path, script: Script) -> tuple[Daemon, asyncio.Task[None]]:
    daemon = Daemon(data_dir=data_dir, provider=MockProvider(default=script))
    task = asyncio.create_task(daemon.run())
    for _ in range(100):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(daemon: Daemon, task: asyncio.Task[None]) -> None:
    daemon._shutdown_event.set()
    await asyncio.wait_for(task, timeout=10.0)


class TestParser:
    def test_run_requires_workspace_and_message(self, tmp_path: Path) -> None:
        ns = cli.parse_args(
            ["run", "--workspace", str(tmp_path), "--message", "hello", "--data-dir", str(tmp_path)]
        )
        assert ns.command == "run"
        assert ns.workspace == tmp_path
        assert ns.message == "hello"
        assert ns.data_dir == tmp_path

    def test_run_data_dir_defaults_unset(self, tmp_path: Path) -> None:
        ns = cli.parse_args(["run", "--workspace", str(tmp_path), "--message", "hi"])
        assert ns.data_dir is None

    def test_missing_subcommand_exits(self) -> None:
        with pytest.raises(SystemExit):
            cli.parse_args([])

    def test_attach_is_not_registered(self) -> None:
        with pytest.raises(SystemExit):
            cli.parse_args(["attach", "sess-1"])


class TestHelloAndSpawn:
    def test_hello_matches_the_window(self) -> None:
        assert cli.hello_message("port-file-token") == {
            "type": "hello",
            "token": "port-file-token",
            "version": PROTOCOL_VERSION,
        }

    def test_daemon_argv_is_tstd_without_parent_pid(self, tmp_path: Path) -> None:
        argv = cli.daemon_argv(tmp_path)
        assert argv[:3] == [sys.executable, "-m", "tstd.daemon"]
        assert "--data-dir" in argv
        assert str(tmp_path) in argv
        assert "--parent-pid" not in argv

    def test_spawn_daemon_does_not_bind_or_pass_parent_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_popen(argv: list[str], **kwargs: Any) -> object:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
        cli.spawn_daemon(tmp_path)
        assert "--parent-pid" not in seen["argv"]
        assert "--data-dir" in seen["argv"]
        assert seen["kwargs"].get("start_new_session") is True

    def test_cli_source_never_binds(self) -> None:
        source = Path(cli.__file__).read_text(encoding="utf-8")
        assert "0.0.0.0" not in source
        assert "serve(" not in source
        assert "from websockets.asyncio.server" not in source

    def test_harness_run_signature_unchanged(self) -> None:
        params = list(inspect.signature(harness_run).parameters)
        assert params == ["workspace", "data_dir", "plan"]


class TestLivePortFile:
    def test_missing_file_is_not_live(self, tmp_path: Path) -> None:
        assert cli.live_port_info(tmp_path) is None

    def test_dead_pid_is_not_live(self, tmp_path: Path) -> None:
        write_port_file(tmp_path, 9, "token", pid=999_999_999)
        assert cli.live_port_info(tmp_path) is None

    def test_live_pid_and_token_are_accepted(self, tmp_path: Path) -> None:
        write_port_file(tmp_path, 4321, "live-token", pid=os.getpid())
        info = cli.live_port_info(tmp_path)
        assert info is not None
        assert info["port"] == 4321
        assert info["token"] == "live-token"
        assert info["pid"] == os.getpid()

    def test_empty_token_is_not_live(self, tmp_path: Path) -> None:
        (tmp_path / "port.json").write_text(
            json.dumps({"port": 1, "token": "", "pid": os.getpid()}),
            encoding="utf-8",
        )
        assert cli.live_port_info(tmp_path) is None


class TestRunAgainstMockDaemon:
    async def test_attaches_to_running_daemon_and_prints_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        workspace = _workspace(tmp_path)
        daemon, task = await _start_daemon(
            data_dir, Script(kind="stream", content="hello from tst run")
        )
        try:
            code = await cli.run_turn(workspace, "say hi", data_dir)
        finally:
            await _stop_daemon(daemon, task)
        assert code == 0
        out = capsys.readouterr().out
        assert "hello from tst run" in out
        assert "assistant_delta" not in out
        assert '"type"' not in out

    async def test_failed_turn_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        workspace = _workspace(tmp_path)
        daemon, task = await _start_daemon(
            data_dir,
            Script(kind="error", error_code="auth_failed", status_code=401, content="nope"),
        )
        try:
            code = await cli.run_turn(workspace, "say hi", data_dir)
        finally:
            await _stop_daemon(daemon, task)
        assert code == 1
        err = capsys.readouterr().err
        assert "auth_failed" in err

    async def test_opens_a_session_when_daemon_already_has_one(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        first = tmp_path / "first"
        first.mkdir()
        second = tmp_path / "second"
        second.mkdir()
        daemon, task = await _start_daemon(data_dir, Script(kind="stream", content="second door"))
        try:
            await daemon._start_session(str(first))
            assert len(await daemon.session_registry.list_sessions()) == 1
            code = await cli.run_turn(second, "go", data_dir)
            sessions = await daemon.session_registry.list_sessions()
        finally:
            await _stop_daemon(daemon, task)
        assert code == 0
        assert len(sessions) == 2
        workspaces = {s.workspace_path for s in sessions}
        assert str(second) in workspaces

    async def test_missing_workspace_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        daemon, task = await _start_daemon(data_dir, Script(kind="stream", content="unused"))
        try:
            code = await cli.run_turn(tmp_path / "nope", "hi", data_dir)
        finally:
            await _stop_daemon(daemon, task)
        assert code == 1
        assert "not a directory" in capsys.readouterr().err

    async def test_stale_port_file_starts_a_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        write_port_file(data_dir, 1, "stale-token", pid=999_999_999)
        workspace = _workspace(tmp_path)
        daemon = Daemon(
            data_dir=data_dir,
            provider=MockProvider(default=Script(kind="stream", content="spawned")),
        )
        run_task: asyncio.Task[None] | None = None

        def fake_spawn(path: Path) -> None:
            nonlocal run_task
            assert path == data_dir
            run_task = asyncio.get_running_loop().create_task(daemon.run())

        monkeypatch.setattr(cli, "spawn_daemon", fake_spawn)
        try:
            code = await cli.run_turn(workspace, "hi", data_dir)
        finally:
            if run_task is not None:
                await _stop_daemon(daemon, run_task)
        assert run_task is not None
        assert code == 0
        assert "spawned" in capsys.readouterr().out


def test_main_dispatches_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Path, str, Path]] = []

    async def fake_run(workspace: Path, message: str, data_dir: Path) -> int:
        seen.append((workspace, message, data_dir))
        return 0

    monkeypatch.setattr(cli, "run_turn", fake_run)
    data = tmp_path / "data"
    data.mkdir()
    code = cli.main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--message",
            "from argv",
            "--data-dir",
            str(data),
        ]
    )
    assert code == 0
    assert seen == [(tmp_path.resolve(), "from argv", data.resolve())]
