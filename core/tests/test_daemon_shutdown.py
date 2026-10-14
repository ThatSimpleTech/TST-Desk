"""``--data-dir`` owns the log file, and SIGTERM ends the daemon (TD-4842).

Readiness is the daemon's own ``ws server started`` line on stdout. The
reader blocks on that line. Nothing here sleeps and then assumes the
process is up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from tests.machine_helpers import ask, eventually, window
from tstd.cli import daemon_argv
from tstd.daemon import Daemon
from tstd.logging import LOG_FILE_NAME, log_directory, user_data_dir
from tstd.mock import MockProvider, Script
from tstd.protocol import SessionState
from tstd.provider import ChatMessage
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore
from tstd.shutdown_budget import SHUTDOWN_BUDGET_SECONDS, ShutdownBudget
from tstd.ws import create_port_file_path

_CORE = Path(__file__).resolve().parent.parent
_CANARY = "sk-ant-api03-CANARYCANARYCANARY"
# Not this process, and not a pid a test daemon will be assigned.
_FOREIGN_PID = 2_147_483_646


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(_CORE),
        env=env,
    )


def _wait_until_serving(proc: subprocess.Popen[bytes], seconds: float = 10.0) -> None:
    """Block until the daemon logs that its websocket server is up."""
    assert proc.stdout is not None
    ready = threading.Event()
    chunks: list[bytes] = []

    def _reader() -> None:
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                return
            chunks.append(line)
            if b"ws server started" in line:
                ready.set()

    threading.Thread(target=_reader, name="tstd-stdout", daemon=True).start()
    if ready.wait(seconds):
        return
    tail = b"".join(chunks).decode(errors="replace")[-2000:]
    raise RuntimeError(f"daemon not ready (exit={proc.poll()}): {tail}")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except PermissionError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return
    if proc.stdout is not None:
        proc.stdout.close()


def _read_log(data_dir: Path) -> str:
    return (log_directory(data_dir) / LOG_FILE_NAME).read_text(encoding="utf-8")


def _shutting_down_reasons(text: str) -> list[object]:
    """The ``reason`` of every "shutting down" line in a JSON log (TD-5021)."""
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [line.get("reason") for line in lines if line.get("message") == "shutting down"]


def _rewrite_pid(port: Path, pid: int) -> None:
    data = json.loads(port.read_text(encoding="utf-8"))
    data["pid"] = pid
    tmp = port.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, port)


class TestDataDirLogs:
    def test_data_dir_flag_keeps_logs_out_of_the_user_dir(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "scratch"
        user_log = log_directory(user_data_dir()) / LOG_FILE_NAME
        proc = _spawn([*daemon_argv(data_dir), "--log-level", "INFO"])
        try:
            _wait_until_serving(proc)
            text = _read_log(data_dir)
            assert "starting" in text
            assert str(data_dir) in text
            assert not user_log.exists()
        finally:
            _stop(proc)

    def test_default_log_dir_is_the_user_data_dir(self, tmp_path: Path) -> None:
        data_dir = user_data_dir()
        proc = _spawn([sys.executable, "-m", "tstd.daemon", "--log-level", "INFO"])
        try:
            _wait_until_serving(proc)
            text = _read_log(data_dir)
            assert "starting" in text
            assert str(data_dir) in text
            assert not (tmp_path / "logs" / LOG_FILE_NAME).exists()
        finally:
            _stop(proc)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows keeps the websocket shutdown path; SIGTERM is not installed",
)
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_daemon_and_removes_port_file(tmp_path: Path, sig: int) -> None:
    data_dir = tmp_path / "scratch"
    proc = _spawn(daemon_argv(data_dir))
    try:
        _wait_until_serving(proc)
        port = create_port_file_path(data_dir)
        assert port.is_file()
        try:
            os.kill(proc.pid, sig)
        except PermissionError:
            pytest.skip("the OS vetoed the test's own kill (TD-605)")
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("daemon still running 5s after the signal") from None
        assert code == 0
        assert not port.exists()
        text = _read_log(data_dir)
        assert "signal received" in text
        assert "shutdown complete" in text
        assert _shutting_down_reasons(text) == ["signal"]
    finally:
        _stop(proc)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows keeps the websocket shutdown path; SIGTERM is not installed",
)
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_leaves_a_port_file_that_names_another_process(tmp_path: Path, sig: int) -> None:
    data_dir = tmp_path / "scratch"
    proc = _spawn(daemon_argv(data_dir))
    try:
        _wait_until_serving(proc)
        port = create_port_file_path(data_dir)
        _rewrite_pid(port, _FOREIGN_PID)
        try:
            os.kill(proc.pid, sig)
        except PermissionError:
            pytest.skip("the OS vetoed the test's own kill (TD-605)")
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise AssertionError("daemon still running 5s after the signal") from None
        assert code == 0
        kept = json.loads(port.read_text(encoding="utf-8"))
        assert kept["pid"] == _FOREIGN_PID
    finally:
        _stop(proc)


class TestShutdownBudget:
    def test_expire_removes_the_port_file_and_exits_once(self, tmp_path: Path) -> None:
        port = create_port_file_path(tmp_path)
        # The canary is the token. Expiry used to unlink whatever bytes
        # were at the path; it now deletes the file only when the pid
        # names this process, and still must not log the token.
        port.write_text(
            json.dumps({"port": 1, "token": _CANARY, "pid": os.getpid()}),
            encoding="utf-8",
        )
        (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
        codes: list[int] = []
        budget = ShutdownBudget(exit_process=codes.append)
        budget._data_dir = tmp_path
        budget._expire()
        budget._expire()
        assert codes == [1]
        assert not port.exists()
        assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep"
        text = _read_log(tmp_path)
        assert "shutdown exceeded the 5s budget" in text
        assert _CANARY not in text

    def test_expire_leaves_a_foreign_port_file(self, tmp_path: Path) -> None:
        port = create_port_file_path(tmp_path)
        port.write_text(
            json.dumps({"port": 1, "token": _CANARY, "pid": _FOREIGN_PID}),
            encoding="utf-8",
        )
        codes: list[int] = []
        budget = ShutdownBudget(exit_process=codes.append)
        budget._data_dir = tmp_path
        budget._expire()
        assert codes == [1]
        kept = json.loads(port.read_text(encoding="utf-8"))
        assert kept["pid"] == _FOREIGN_PID
        assert kept["token"] == _CANARY
        assert _CANARY not in _read_log(tmp_path)

    def test_finish_leaves_the_port_file(self, tmp_path: Path) -> None:
        port = create_port_file_path(tmp_path)
        port.write_text("{}", encoding="utf-8")
        codes: list[int] = []
        budget = ShutdownBudget(seconds=30, exit_process=codes.append)
        try:
            budget.arm(tmp_path)
            assert budget.pending
            budget.finish()
            budget._expire()
            assert codes == []
            assert port.is_file()
            assert not budget.pending
        finally:
            budget.finish()

    def test_arm_schedules_one_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_timer = threading.Timer
        seen: list[tuple[float, object]] = []

        def tracking(interval: float, function: object) -> threading.Timer:
            seen.append((interval, function))
            return real_timer(interval, function)  # type: ignore[arg-type]

        monkeypatch.setattr("tstd.shutdown_budget.threading.Timer", tracking)
        codes: list[int] = []
        budget = ShutdownBudget(exit_process=codes.append)
        try:
            budget.arm(tmp_path)
            budget.arm(tmp_path)
            assert seen == [(SHUTDOWN_BUDGET_SECONDS, budget._expire)]
            assert budget.pending
        finally:
            budget.finish()
        assert codes == []

    def test_windows_install_does_not_need_a_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tstd.shutdown_budget import install_posix_shutdown_signals

        monkeypatch.setattr(sys, "platform", "win32")
        install_posix_shutdown_signals(lambda: None)


# ── why it stopped (TD-5018, TD-5021) ────────────────────────────────


def _quiet_budget() -> ShutdownBudget:
    """A real budget that cannot end the pytest process if it fires."""
    return ShutdownBudget(exit_process=lambda _code: None)


async def _reasons_for(
    daemon: Daemon,
    trigger: Callable[[], Awaitable[None]],
    caplog: pytest.LogCaptureFixture,
) -> list[object]:
    """Run *daemon*, pull *trigger*, and read every "shutting down" reason."""
    with caplog.at_level(logging.INFO, logger="tstd.daemon"):
        runner = asyncio.create_task(daemon.run())
        await eventually(lambda: daemon.ws_server.port > 0 or runner.done(), within=10)
        try:
            await trigger()
            await asyncio.wait_for(runner, timeout=10)
        finally:
            if not runner.done():
                daemon._shutdown_event.set()
                await asyncio.wait_for(runner, timeout=10)
    return [
        record.__dict__.get("extra_fields", {}).get("reason")
        for record in caplog.records
        if record.getMessage() == "shutting down"
    ]


class TestShutdownReason:
    async def test_websocket(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        daemon = Daemon(data_dir=tmp_path / "d", shutdown_budget=_quiet_budget())

        async def trigger() -> None:
            async with window(daemon) as ws:
                await ws.send(json.dumps({"type": "shutdown"}))
                await eventually(daemon._shutdown_event.is_set)

        assert await _reasons_for(daemon, trigger, caplog) == ["websocket"]

    async def test_host_gone(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        sleeper = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)"
        )
        daemon = Daemon(
            data_dir=tmp_path / "d", parent_pid=sleeper.pid, shutdown_budget=_quiet_budget()
        )
        daemon._parent_poll_interval = 0.05

        async def trigger() -> None:
            try:
                sleeper.kill()
            except PermissionError:
                pytest.skip("the OS vetoed the test's own kill (TD-605)")
            await asyncio.wait_for(sleeper.wait(), 5)

        try:
            assert await _reasons_for(daemon, trigger, caplog) == ["host_gone"]
        finally:
            if sleeper.returncode is None:
                sleeper.kill()
                await asyncio.wait_for(sleeper.wait(), 5)

    async def test_shutdown_during_a_drain_hold_is_the_drain(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon = Daemon(data_dir=tmp_path / "d", shutdown_budget=_quiet_budget())

        async def trigger() -> None:
            async with window(daemon) as ws:
                held = await ask(
                    ws, {"type": "drain", "timeout_s": 0, "hold_s": 60}, "drain_result"
                )
                assert held["drained"] is True
                await ws.send(json.dumps({"type": "shutdown"}))
                await eventually(daemon._shutdown_event.is_set)

        assert await _reasons_for(daemon, trigger, caplog) == ["drain"]

    async def test_shutdown_cancels_sessions_for_the_daemon(self, tmp_path: Path) -> None:
        """TD-5018: the cancelled state says the daemon stopped, not the user."""
        data_dir = tmp_path / "d"
        workspace = tmp_path / "ws"
        data_dir.mkdir()
        workspace.mkdir()
        await SessionStore(data_dir).upsert("sid-1", str(workspace), "running")
        persist = SessionPersist(data_dir)
        persist.prepare("sid-1")
        persist.save_conversation("sid-1", [ChatMessage(role="user", content="hi")])
        daemon = Daemon(
            data_dir=data_dir,
            provider=MockProvider(default=Script(kind="stream")),
            shutdown_budget=_quiet_budget(),
        )
        await daemon._restore_sessions()
        sess = daemon.session_registry.get("sid-1")
        assert sess is not None and sess.state == "running"
        await daemon._shutdown()
        loaded = SessionPersist(data_dir).load("sid-1")
        assert loaded is not None
        states = [e for e in loaded.events if isinstance(e, SessionState)]
        assert states[-1].state == "cancelled"
        assert states[-1].reason == "daemon shutdown"
