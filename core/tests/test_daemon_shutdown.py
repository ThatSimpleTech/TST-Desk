"""``--data-dir`` owns the log file, and SIGTERM ends the daemon (TD-4842).

Readiness is the daemon's own ``ws server started`` line on stdout. The
reader blocks on that line. Nothing here sleeps and then assumes the
process is up.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tstd.cli import daemon_argv
from tstd.logging import LOG_FILE_NAME, log_directory, user_data_dir
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
