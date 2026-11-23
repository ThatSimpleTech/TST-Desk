"""TD-5294: the sandbox seam reports what it enforces, and callers act on it.

The probe tries bubblewrap for real and reports full, partial (a promise
it cannot hold here, named) or none (with the fix). Only full lets a
command skip its ask; the card, the result note and Doctor all say which.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_shell_sandbox import _session, needs_bwrap
from tstd import shell_sandbox
from tstd.daemon import _check_sandbox
from tstd.sandbox import (
    BackendStatus,
    Enforcement,
    cached_status,
    current_status,
    reprobe,
    set_backend,
)
from tstd.sandbox import bwrap as bwrap_mod
from tstd.sandbox.backend import approval_view
from tstd.sandbox.bwrap import BwrapBackend
from tstd.shell_sandbox import SandboxSection
from tstd.tools import run_shell

FULL = BackendStatus("bwrap", Enforcement.FULL)
PARTIAL = BackendStatus("bwrap", Enforcement.PARTIAL, gaps=(("pins", "nested namespaces"),))
NONE = BackendStatus("bwrap", Enforcement.NONE, unavailable="bubblewrap is not installed")


class _Counted:
    name = "counted"

    def __init__(self, status: BackendStatus) -> None:
        self.status = status
        self.probes = 0

    def probe(self) -> BackendStatus:
        self.probes += 1
        return self.status


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    set_backend(None)
    yield
    set_backend(None)


def _answers(monkeypatch: pytest.MonkeyPatch, start: int, nested: int | None) -> None:
    """bwrap is on PATH; starting it exits *start*, the nested check *nested*."""
    monkeypatch.setattr(bwrap_mod.sys, "platform", "linux")
    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda _n: "/usr/bin/bwrap")
    monkeypatch.setattr(shell_sandbox, "_seccomp_arch", lambda: (0xC000003E, 41))

    def run(argv: list[str]) -> subprocess.CompletedProcess[bytes] | None:
        code = nested if argv[-1] == bwrap_mod._NESTED else start
        if code is None:
            return None
        return subprocess.CompletedProcess(argv, code, b"", b"bwrap: no permissions")

    monkeypatch.setattr(bwrap_mod, "_run", run)


class TestProbe:
    def test_full_when_it_starts_and_no_namespace_can_undo_a_bind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answers(monkeypatch, 0, 1)
        assert BwrapBackend().probe() == FULL

    def test_partial_when_a_nested_namespace_maps_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _answers(monkeypatch, 0, 0)
        got = BwrapBackend().probe()
        assert got.enforcement is Enforcement.PARTIAL and [k for k, _ in got.gaps] == ["pins"]
        assert got.unavailable is None

    @pytest.mark.parametrize("nested", [127, None])
    def test_partial_when_that_cannot_be_checked(
        self, monkeypatch: pytest.MonkeyPatch, nested: int | None
    ) -> None:
        _answers(monkeypatch, 0, nested)
        got = BwrapBackend().probe()
        assert got.enforcement is Enforcement.PARTIAL and "could not be checked" in got.gap_text()

    def test_none_when_it_cannot_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _answers(monkeypatch, 1, 1)
        got = BwrapBackend().probe()
        assert got.enforcement is Enforcement.NONE
        assert got.unavailable is not None and "no permissions" in got.unavailable

    def test_none_without_bubblewrap_or_off_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _answers(monkeypatch, 0, 1)
        monkeypatch.setattr(bwrap_mod.shutil, "which", lambda _n: None)
        assert "not installed" in (BwrapBackend().probe().unavailable or "")
        monkeypatch.setattr(bwrap_mod.sys, "platform", "darwin")
        assert "needs Linux" in (BwrapBackend().probe().unavailable or "")

    @needs_bwrap
    def test_this_host_runs_it(self) -> None:
        assert current_status().enforcement is not Enforcement.NONE


class TestCache:
    def test_probed_once_until_asked_again(self) -> None:
        backend = _Counted(FULL)
        set_backend(backend)
        assert cached_status() is None
        assert current_status() == current_status() == FULL
        assert backend.probes == 1 and cached_status() == FULL
        backend.status = NONE
        assert reprobe() == NONE and backend.probes == 2
        assert shell_sandbox.unavailable_reason() == NONE.unavailable


class TestWhatItSays:
    def test_card_lines(self) -> None:
        assert approval_view(FULL, applies=True) == (
            "full",
            "bwrap",
            False,
            [],
            "Runs in the sandbox (bwrap, full)",
        )
        level, _, _, gaps, line = approval_view(PARTIAL, applies=True) or ("", "", 0, [], "")
        assert level == "partial" and "pins not enforced" in line and len(gaps) == 1
        assert approval_view(NONE, applies=True) is not None
        assert "refused" in approval_view(NONE, applies=True)[4]  # type: ignore[index]
        outside = approval_view(None, applies=False)
        assert outside is not None and outside[2] is True and "outside" in outside[4]
        # Not probed yet and it applies: say nothing rather than guess.
        assert approval_view(None, applies=True) is None

    def test_result_note_names_backend_and_level(self) -> None:
        assert shell_sandbox.note(FULL).startswith("ran in the shell sandbox (bwrap, full)")
        partial = shell_sandbox.note(PARTIAL)
        assert "(bwrap, partial)" in partial and "nested namespaces" in partial

    @pytest.mark.parametrize(("status", "row"), [(FULL, "ok"), (PARTIAL, "warn"), (NONE, "skip")])
    def test_doctor_row(self, status: BackendStatus, row: str) -> None:
        backend = _Counted(status)
        set_backend(backend)
        current_status()
        check = _check_sandbox()
        assert check.name == "sandbox" and check.status == row and check.fix is None
        # Doctor probes afresh, so installing bubblewrap needs no restart.
        assert backend.probes == 2

    def test_the_approval_card_carries_it_for_a_shell_call(self, tmp_path: Path) -> None:
        set_backend(_Counted(PARTIAL))
        current_status()
        on = _session(tmp_path, SandboxSection(enabled=True, exclude=["docker *"]))
        card: Any = on._approval_sandbox("shell", {"command": "make"})
        assert card.level == "partial" and card.backend == "bwrap" and not card.outside
        host: Any = on._approval_sandbox("shell", {"command": "docker ps"})
        assert host.outside and host.level == "none"
        assert on._approval_sandbox("fs_write", {"path": "a"}) is None
        on.autonomy = True
        assert on._approval_sandbox("shell", {"command": "make"}) is None


class TestPromiseHeld:
    async def test_a_promised_run_is_refused_once_the_sandbox_is_weaker(
        self, tmp_path: Path
    ) -> None:
        set_backend(_Counted(PARTIAL))
        session = _session(tmp_path, SandboxSection(enabled=True, auto_allow=True))
        session.bwrap_promised.add("call-1")
        with pytest.raises(ValueError, match="no longer does"):
            await run_shell(session, "touch made.txt", tool_call_id="call-1")
        assert not (tmp_path / "made.txt").exists()
