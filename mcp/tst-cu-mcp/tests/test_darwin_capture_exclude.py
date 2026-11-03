"""Local captures leave agent-desktop's cursor windows out (TD-5227).

agent-desktop draws each agent's cursor in ordinary on-screen windows. A
screenshot must not show them: the model would see its own cursor, and one
agent would see another's. The fake stands in for pyobjc's Quartz, so these
run without macOS.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, cast

import pytest

from tst_cu_mcp.backends import darwin
from tst_cu_mcp.backends.darwin_capture import kept_window_ids, screen_image

PLAIN = "plain image"
FROM_ARRAY = "from-array image"
RECT = (0.0, 0.0, 800.0, 600.0)


class FakeQuartz:
    kCGWindowListOptionOnScreenOnly = 1
    kCGNullWindowID = 0
    kCGWindowImageDefault = 0

    def __init__(self, windows: list[tuple[int, str]], *, described: bool = True) -> None:
        self.windows = windows
        self.described = described
        self.from_array_result: Any = FROM_ARRAY
        self.list_error: Exception | None = None
        self.calls: list[tuple[str, Any]] = []

    def CGWindowListCreate(self, option: int, relative_to: int) -> tuple[int, ...]:
        if self.list_error is not None:
            raise self.list_error
        assert (option, relative_to) == (1, 0)
        return tuple(window_id for window_id, _ in self.windows)

    def CGWindowListCreateDescriptionFromArray(self, ids: tuple[int, ...]) -> list[dict[str, Any]]:
        listed = [
            {"kCGWindowNumber": window_id, "kCGWindowOwnerName": owner}
            for window_id, owner in self.windows
            if window_id in ids
        ]
        # Real descriptions skip windows that closed after the id list was read.
        return listed if self.described else listed[1:]

    def CGWindowListCreateImageFromArray(self, rect: Any, ids: list[int], option: int) -> Any:
        self.calls.append(("from_array", list(ids)))
        return self.from_array_result

    def CGWindowListCreateImage(self, rect: Any, option: int, window: int, image: int) -> Any:
        self.calls.append(("plain", (option, window, image)))
        return PLAIN

    def CGRectMake(self, x: float, y: float, w: float, h: float) -> tuple[float, ...]:
        return (x, y, w, h)

    def CGImageGetWidth(self, _image: Any) -> int:
        return 800

    def CGImageGetHeight(self, _image: Any) -> int:
        return 600


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeQuartz) -> FakeQuartz:
    monkeypatch.setitem(sys.modules, "Quartz", cast(ModuleType, fake))
    return fake


DESKTOP = [(3, "Window Server"), (40, "Finder"), (12, "TST Desk"), (2, "Dock")]


def test_kept_ids_drop_agent_desktop_and_keep_the_order() -> None:
    windows = [
        (91, "agent-desktop"),
        (40, "Finder"),
        (92, "agent-desktop"),
        (12, "TST Desk"),
        (3, "Window Server"),
    ]
    assert kept_window_ids(windows) == [40, 12, 3]


def test_only_the_exact_owner_name_is_dropped() -> None:
    windows = [(1, "agent-desktop-helper"), (2, "Agent-Desktop"), (3, " agent-desktop"), (4, "")]
    assert kept_window_ids(windows) == [1, 2, 3, 4]


def test_no_agent_desktop_window_keeps_the_plain_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeQuartz(DESKTOP))

    assert screen_image(RECT) == PLAIN
    assert fake.calls == [("plain", (1, 0, 0))]


def test_agent_desktop_windows_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeQuartz([(91, "agent-desktop"), *DESKTOP]))

    assert screen_image(RECT) == FROM_ARRAY
    assert fake.calls == [("from_array", [3, 40, 12, 2])]


def test_a_null_from_array_image_falls_back_to_the_plain_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, FakeQuartz([(91, "agent-desktop"), *DESKTOP]))
    fake.from_array_result = None

    assert screen_image(RECT) == PLAIN
    assert [name for name, _ in fake.calls] == ["from_array", "plain"]


def test_an_unreadable_window_list_keeps_the_plain_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, FakeQuartz([(91, "agent-desktop"), *DESKTOP]))
    fake.list_error = RuntimeError("window server busy")

    assert screen_image(RECT) == PLAIN
    assert [name for name, _ in fake.calls] == ["plain"]


def test_a_window_without_a_description_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(
        monkeypatch, FakeQuartz([(7, "Safari"), (91, "agent-desktop"), *DESKTOP], described=False)
    )

    assert screen_image(RECT) == FROM_ARRAY
    assert fake.calls == [("from_array", [7, 3, 40, 12, 2])]


def test_the_local_capture_path_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeQuartz([(91, "agent-desktop"), *DESKTOP]))
    encoded: list[Any] = []

    def png(image: Any) -> bytes:
        encoded.append(image)
        return b"\x89PNG fake"

    monkeypatch.setattr(darwin, "_png_from_cgimage", png)

    assert darwin._cg_capture_png((0, 0, 800, 600)) == b"\x89PNG fake"
    assert encoded == [FROM_ARRAY]
    assert fake.calls == [("from_array", [3, 40, 12, 2])]


def test_a_bundled_copy_is_found_by_its_executable_not_its_owner_name() -> None:
    """TD-5231: inside an app bundle agent-desktop's windows report the app's
    name as their owner. The app's own window, same owner name, stays."""
    from tst_cu_mcp.backends.darwin_capture import _owners

    described = [
        {"kCGWindowNumber": 7, "kCGWindowOwnerName": "TST Desk", "kCGWindowOwnerPID": 501},
        {"kCGWindowNumber": 8, "kCGWindowOwnerName": "TST Desk", "kCGWindowOwnerPID": 777},
        {"kCGWindowNumber": 9, "kCGWindowOwnerName": "Finder", "kCGWindowOwnerPID": 300},
    ]
    names = {501: "tst-desk", 777: "agent-desktop", 300: "Finder"}
    asked: list[int] = []

    def process_name(pid: int) -> str:
        asked.append(pid)
        return names[pid]

    windows = _owners([7, 8, 9], described, process_name)
    assert kept_window_ids(windows) == [7, 9]
    assert sorted(asked) == [300, 501, 777]


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS")
def test_process_name_reads_this_process() -> None:
    import os

    from tst_cu_mcp.backends.darwin_capture import process_name

    assert process_name(os.getpid()) == os.path.basename(os.path.realpath(sys.executable))
    assert process_name(-1) == ""
