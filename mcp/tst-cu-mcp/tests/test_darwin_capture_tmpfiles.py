"""Screenshot temp files are written, read, and gone — within the same call.

``capture_png`` round-trips through a temp file because ``screencapture`` only
writes to paths. The cleanup lives in a ``finally``; these tests prove it holds
on the success path, on an empty capture, and when screencapture itself fails,
so a burst of failed screenshots can never leave ``tstcu-*.png`` behind.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp.backends.darwin import DarwinBackend


@pytest.fixture
def temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point mkstemp at tmp_path so any leftover file is visible."""
    real_mkstemp = tempfile.mkstemp

    def scoped(*args: Any, **kwargs: Any) -> tuple[int, str]:
        kwargs["dir"] = str(tmp_path)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", scoped)
    return tmp_path


def test_success_leaves_no_temp_file(temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\nstub"

    def write_image(_self: object, _rect: tuple[int, int, int, int], out_path: Path) -> None:
        out_path.write_bytes(png)

    monkeypatch.setattr(DarwinBackend, "_run_screencapture", write_image)
    assert DarwinBackend().capture_png((0, 0, 10, 10)) == png
    assert list(temp_dir.iterdir()) == []


def test_consecutive_calls_do_not_accumulate(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write_image(_self: object, _rect: tuple[int, int, int, int], out_path: Path) -> None:
        out_path.write_bytes(b"\x89PNG\r\n\x1a\nstub")

    monkeypatch.setattr(DarwinBackend, "_run_screencapture", write_image)
    backend = DarwinBackend()
    for _ in range(3):
        assert backend.capture_png((0, 0, 10, 10)).startswith(b"\x89PNG")
    # Each call cleaned up after itself; none waited for a final sweep.
    assert list(temp_dir.iterdir()) == []


def test_empty_capture_raises_and_still_cleans_up(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write_nothing(_self: object, _rect: tuple[int, int, int, int], out_path: Path) -> None:
        out_path.write_bytes(b"")

    monkeypatch.setattr(DarwinBackend, "_run_screencapture", write_nothing)
    with pytest.raises(RuntimeError, match="no image"):
        DarwinBackend().capture_png((0, 0, 10, 10))
    assert list(temp_dir.iterdir()) == []


def test_failed_screencapture_leaves_no_temp_file(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_self: object, _rect: tuple[int, int, int, int], _out_path: Path) -> None:
        raise RuntimeError("screencapture failed (rc=1)")

    monkeypatch.setattr(DarwinBackend, "_run_screencapture", fail)
    with pytest.raises(RuntimeError, match="failed"):
        DarwinBackend().capture_png((0, 0, 10, 10))
    assert list(temp_dir.iterdir()) == []
