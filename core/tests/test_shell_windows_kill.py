"""Windows tree-kill helper (TD-1406).

These tests run on every host. They prove the argv and the refusal
notes, not that taskkill actually reaped a tree — that still needs a
Windows machine.
"""

from __future__ import annotations

import subprocess

import pytest

from tstd.tools import shell


def test_taskkill_argv_is_tree_force_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)  # type: ignore[arg-type]
    assert shell._kill_windows_tree(4242) is None
    assert seen == [["taskkill", "/T", "/F", "/PID", "4242"]]


def test_already_gone_is_not_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, shell._TASKKILL_NOT_FOUND, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)  # type: ignore[arg-type]
    assert shell._kill_windows_tree(1) is None


def test_nonzero_exit_is_a_refusal_note(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, b"", b"Access denied\n")

    monkeypatch.setattr(subprocess, "run", fake_run)  # type: ignore[arg-type]
    note = shell._kill_windows_tree(1)
    assert note is not None
    assert "refused" in note
    assert "Access denied" in note


def test_missing_taskkill_is_a_refusal_note(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(subprocess, "run", fake_run)  # type: ignore[arg-type]
    note = shell._kill_windows_tree(1)
    assert note == "taskkill not found; only the direct child was terminated"


def test_timeout_is_a_refusal_note(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 2)

    monkeypatch.setattr(subprocess, "run", fake_run)  # type: ignore[arg-type]
    note = shell._kill_windows_tree(1)
    assert note is not None
    assert "timed out" in note


def test_process_group_on_windows_walks_the_tree_before_killing_the_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TerminateProcess on the leader first orphans grandchildren (TD-1406)."""
    monkeypatch.setattr(shell.sys, "platform", "win32")
    order: list[str] = []

    class _Proc:
        pid = 99

        def kill(self) -> None:
            order.append("kill")

    def fake_tree(pid: int) -> str | None:
        order.append(f"tree:{pid}")
        return None

    monkeypatch.setattr(shell, "_kill_windows_tree", fake_tree)
    assert shell._kill_process_group(_Proc()) is None  # type: ignore[arg-type]
    assert order == ["tree:99", "kill"]


def test_creation_flag_is_the_stdlib_constant_on_windows() -> None:
    """The spawn flag must be the real Win32 bit, not a silent 0, on win32."""
    if shell.sys.platform == "win32":
        assert shell._CREATE_NEW_PROCESS_GROUP == subprocess.CREATE_NEW_PROCESS_GROUP
        assert shell._CREATE_NEW_PROCESS_GROUP != 0
    else:
        assert shell._CREATE_NEW_PROCESS_GROUP == 0
