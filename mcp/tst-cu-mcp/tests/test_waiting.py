"""Bounded waits, and waiting on a condition rather than a duration.

`wait_for_window` is driven against a scripted sequence of foreground windows, so
its polling and timeout behaviour is provable without launching anything or
actually sleeping for the timeout.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tst_cu_mcp import waiting
from tst_cu_mcp.focus import WindowInfo

NOTEPAD = WindowInfo(
    title="Untitled - Notepad", process="notepad.exe", pid=1, x=0, y=0, width=8, height=6
)
CHROME = WindowInfo(
    title="New Tab - Google Chrome", process="chrome.exe", pid=2, x=0, y=0, width=8, height=6
)


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record sleeps instead of taking them, so the suite stays fast."""
    slept: list[float] = []
    monkeypatch.setattr("tst_cu_mcp.waiting.time.sleep", lambda s: slept.append(s))
    return slept


def script(*windows: WindowInfo) -> Iterator[WindowInfo]:
    """Yield each window once, then repeat the last forever."""
    yield from windows[:-1]
    while True:
        yield windows[-1]


class TestWait:
    def test_returns_what_it_waited(self, no_real_sleeping: list[float]) -> None:
        assert waiting.wait(1.5) == {"waited_seconds": 1.5}
        assert no_real_sleeping == [1.5]

    def test_zero_is_allowed(self) -> None:
        assert waiting.wait(0)["waited_seconds"] == 0

    def test_negative_is_refused(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            waiting.wait(-1)

    def test_over_the_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="wait_for_window"):
            waiting.wait(waiting.MAX_WAIT_SECONDS + 1)

    def test_the_cap_itself_is_allowed(self) -> None:
        assert waiting.wait(waiting.MAX_WAIT_SECONDS)["waited_seconds"] == pytest.approx(
            waiting.MAX_WAIT_SECONDS
        )

    def test_refusal_points_at_the_better_tool(self) -> None:
        # An unbounded wait is a wedged session, so the error has to offer the
        # alternative rather than just saying no.
        with pytest.raises(ValueError, match="rather than a duration"):
            waiting.wait(999)


class TestWaitForWindow:
    def test_immediate_match_does_not_sleep(
        self, monkeypatch: pytest.MonkeyPatch, no_real_sleeping: list[float]
    ) -> None:
        # Checks once before sleeping at all, so an already-correct window is free.
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: NOTEPAD)
        result = waiting.wait_for_window("Notepad", timeout_seconds=5)
        assert result["matched"] is True
        assert no_real_sleeping == []

    def test_match_after_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sequence = script(CHROME, CHROME, NOTEPAD)
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: next(sequence))
        result = waiting.wait_for_window("Notepad", timeout_seconds=5)
        assert result["matched"] is True

    def test_reports_the_window_it_matched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: NOTEPAD)
        result = waiting.wait_for_window("notepad.exe")
        assert result["foreground_window"]["process"] == "notepad.exe"

    def test_timeout_returns_rather_than_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A timeout is information to act on, not an exception that discards it.
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: CHROME)
        result = waiting.wait_for_window("Notepad", timeout_seconds=0)
        assert result["matched"] is False

    def test_timeout_reports_what_is_actually_in_front(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: CHROME)
        result = waiting.wait_for_window("Notepad", timeout_seconds=0)
        assert result["foreground_window"]["process"] == "chrome.exe"
        assert result["expected"] == "Notepad"

    def test_timeout_hint_lists_the_plausible_causes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: CHROME)
        hint = waiting.wait_for_window("Notepad", timeout_seconds=0)["hint"]
        assert "failed to" in hint
        assert "took focus" in hint

    def test_no_hint_when_it_matched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: NOTEPAD)
        assert "hint" not in waiting.wait_for_window("Notepad")

    @pytest.mark.parametrize("title", ["", "   "])
    def test_blank_title_is_refused(self, title: str) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            waiting.wait_for_window(title)

    def test_negative_timeout_is_refused(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            waiting.wait_for_window("Notepad", timeout_seconds=-1)

    def test_timeout_over_the_cap_is_refused(self) -> None:
        with pytest.raises(ValueError, match="<="):
            waiting.wait_for_window("Notepad", timeout_seconds=waiting.MAX_WAIT_SECONDS + 1)

    def test_non_positive_poll_interval_is_refused(self) -> None:
        # Zero would spin the CPU at full tilt for the whole timeout.
        with pytest.raises(ValueError, match="> 0"):
            waiting.wait_for_window("Notepad", poll_interval_seconds=0)

    def test_poll_never_sleeps_past_the_timeout(
        self, monkeypatch: pytest.MonkeyPatch, no_real_sleeping: list[float]
    ) -> None:
        monkeypatch.setattr("tst_cu_mcp.waiting.foreground_window", lambda: CHROME)
        waiting.wait_for_window("Notepad", timeout_seconds=0.1, poll_interval_seconds=5)
        assert all(s <= 0.1 for s in no_real_sleeping)
