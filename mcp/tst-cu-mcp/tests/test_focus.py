"""Window identity and the matching policy behind the expect_window guard.

All pure — no desktop. The matcher is the whole policy, so its edge cases are the
guard's edge cases: too strict and the guard is unusable, too loose and it
approves the wrong window.
"""

from __future__ import annotations

import pytest

from tst_cu_mcp.focus import WindowFocusError, WindowInfo, assert_foreground, window_matches

CHROME = WindowInfo(
    title="2026_Engineer Report - Google Docs - Google Chrome",
    process="chrome.exe",
    pid=4242,
    x=0,
    y=0,
    width=1920,
    height=1080,
)
UNTITLED = WindowInfo(title="", process="explorer.exe", pid=7, x=0, y=0, width=10, height=10)
ANONYMOUS = WindowInfo(title="", process="", pid=99, x=0, y=0, width=0, height=0)


class TestWindowMatches:
    @pytest.mark.parametrize(
        "expected",
        ["Google Chrome", "google chrome", "GOOGLE CHROME", "  Google Chrome  "],
    )
    def test_case_and_whitespace_insensitive(self, expected: str) -> None:
        assert window_matches(expected, CHROME) is True

    def test_substring_of_the_title(self) -> None:
        # Real titles carry volatile detail. Requiring the whole string would make
        # the guard unusable on anything with a document name in it.
        assert window_matches("Engineer Report", CHROME) is True

    def test_matches_the_process_name(self) -> None:
        # So a caller who only cares which application has focus can say so.
        assert window_matches("chrome.exe", CHROME) is True

    def test_matches_a_process_name_fragment(self) -> None:
        assert window_matches("chrome", CHROME) is True

    def test_matches_process_when_there_is_no_title(self) -> None:
        assert window_matches("explorer", UNTITLED) is True

    def test_non_match_is_false(self) -> None:
        assert window_matches("Notepad", CHROME) is False

    @pytest.mark.parametrize("expected", ["", "   ", "\t\n"])
    def test_blank_expectation_matches_nothing(self, expected: str) -> None:
        # A blank expectation almost certainly means an unset variable. Matching
        # everything would silently disable the guard, which is the opposite of
        # what it is for.
        assert window_matches(expected, CHROME) is False

    def test_blank_expectation_does_not_match_a_blank_window(self) -> None:
        assert window_matches("", ANONYMOUS) is False

    def test_no_window_matches_nothing_meaningful(self) -> None:
        assert window_matches("anything", ANONYMOUS) is False


class TestWindowInfo:
    def test_to_dict_shape(self) -> None:
        payload = CHROME.to_dict()
        assert set(payload) == {"title", "process", "pid", "bounds_points"}
        assert set(payload["bounds_points"]) == {"x", "y", "width", "height"}

    def test_describe_uses_title_and_process(self) -> None:
        assert "Engineer Report" in CHROME.describe()
        assert "chrome.exe" in CHROME.describe()

    def test_describe_without_a_title(self) -> None:
        assert "explorer.exe" in UNTITLED.describe()

    def test_describe_without_either(self) -> None:
        # Still has to say something identifiable, or the error message is useless.
        assert "99" in ANONYMOUS.describe()


class TestAssertForeground:
    def test_none_skips_the_check_entirely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Cheap but important: the guard is opt-in, so passing None must not cost
        # an OS call.
        called = False

        def spy() -> WindowInfo:
            nonlocal called
            called = True
            return CHROME

        monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", spy)
        assert assert_foreground(None) is None
        assert called is False

    def test_match_returns_the_observed_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", lambda: CHROME)
        assert assert_foreground("chrome") is CHROME

    def test_mismatch_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", lambda: CHROME)
        with pytest.raises(WindowFocusError):
            assert_foreground("Notepad")

    def test_mismatch_names_both_sides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A model that gets this back needs to know what it is looking at, not
        # merely that it was wrong.
        monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", lambda: CHROME)
        with pytest.raises(WindowFocusError) as excinfo:
            assert_foreground("Notepad")
        message = str(excinfo.value)
        assert "Notepad" in message
        assert "chrome.exe" in message

    def test_mismatch_says_nothing_was_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", lambda: CHROME)
        with pytest.raises(WindowFocusError, match="Nothing was sent"):
            assert_foreground("Notepad")

    def test_mismatch_suggests_a_fresh_screenshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tst_cu_mcp.focus.foreground_window", lambda: CHROME)
        with pytest.raises(WindowFocusError, match="fresh screenshot"):
            assert_foreground("Notepad")
