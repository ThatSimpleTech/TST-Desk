"""macOS TCC: probe the host grant, and never re-prompt.

Grok (and other clients) pass ``request=true`` on every ``check_permissions``
call. ``CGRequestScreenCaptureAccess`` then re-shows the dialog even after the
user granted the host app, because this interpreter is not that binary. These
tests pin the two properties that stop that loop: a readable window title
counts as granted, and each prompt is raised at most once.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from tst_cu_mcp.backends import darwin
from tst_cu_mcp.backends.darwin import DarwinBackend
from tst_cu_mcp.permissions import ACCESSIBILITY, SCREEN_RECORDING


@pytest.fixture(autouse=True)
def isolated_prompt_stamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Do not write ``~/.tst-cu-mcp`` from unit tests, and start unprompted."""
    stamp = tmp_path / "macos-tcc-prompted"
    monkeypatch.setattr(darwin, "PROMPT_STAMP_DIR", stamp)
    darwin._prompted_this_process.clear()
    monkeypatch.setattr(darwin, "_sock_path", lambda: None)
    return stamp


def test_window_titles_count_as_screen_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: False)
    monkeypatch.setattr(
        darwin,
        "_window_titles_visible",
        lambda: True,
    )
    monkeypatch.setattr(darwin, "_ax_process_trusted", lambda: True)
    monkeypatch.setattr(darwin, "_ax_api_usable", lambda: False)
    report = DarwinBackend().check_permissions(request=False)
    assert report[SCREEN_RECORDING]["granted"] is True


def test_ax_api_counts_as_accessibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: True)
    monkeypatch.setattr(darwin, "_window_titles_visible", lambda: False)
    monkeypatch.setattr(darwin, "_ax_process_trusted", lambda: False)
    monkeypatch.setattr(darwin, "_ax_api_usable", lambda: True)
    report = DarwinBackend().check_permissions(request=False)
    assert report[ACCESSIBILITY]["granted"] is True


def test_request_true_prompts_each_permission_once(
    monkeypatch: pytest.MonkeyPatch, isolated_prompt_stamp: Path
) -> None:
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: False)
    monkeypatch.setattr(darwin, "_window_titles_visible", lambda: False)
    monkeypatch.setattr(darwin, "_ax_process_trusted", lambda: False)
    monkeypatch.setattr(darwin, "_ax_api_usable", lambda: False)
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: True)
    screen_calls: list[int] = []
    access_calls: list[int] = []

    def request_screen() -> bool:
        screen_calls.append(1)
        return False

    def request_access() -> bool:
        access_calls.append(1)
        return False

    monkeypatch.setattr(darwin, "_request_screen_recording", request_screen)
    monkeypatch.setattr(darwin, "_request_accessibility", request_access)

    DarwinBackend().check_permissions(request=True)
    DarwinBackend().check_permissions(request=True)
    darwin._prompted_this_process.clear()
    DarwinBackend().check_permissions(request=True)

    assert screen_calls == [1]
    assert access_calls == [1]
    assert (isolated_prompt_stamp / SCREEN_RECORDING).is_file()
    assert (isolated_prompt_stamp / ACCESSIBILITY).is_file()


def test_request_true_does_not_prompt_when_already_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: True)
    monkeypatch.setattr(darwin, "_window_titles_visible", lambda: False)
    monkeypatch.setattr(darwin, "_ax_process_trusted", lambda: True)
    monkeypatch.setattr(darwin, "_ax_api_usable", lambda: False)

    def fail_screen() -> bool:
        raise AssertionError("must not prompt screen")

    def fail_access() -> bool:
        raise AssertionError("must not prompt accessibility")

    monkeypatch.setattr(darwin, "_request_screen_recording", fail_screen)
    monkeypatch.setattr(darwin, "_request_accessibility", fail_access)
    report = DarwinBackend().check_permissions(request=True)
    assert report["all_granted"] is True


def test_request_false_never_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: False)
    monkeypatch.setattr(darwin, "_window_titles_visible", lambda: False)
    monkeypatch.setattr(darwin, "_ax_process_trusted", lambda: False)
    monkeypatch.setattr(darwin, "_ax_api_usable", lambda: False)

    def fail_screen() -> bool:
        raise AssertionError("must not prompt screen")

    def fail_access() -> bool:
        raise AssertionError("must not prompt accessibility")

    monkeypatch.setattr(darwin, "_request_screen_recording", fail_screen)
    monkeypatch.setattr(darwin, "_request_accessibility", fail_access)
    report = DarwinBackend().check_permissions(request=False)
    assert report["all_granted"] is False


def _fake_quartz(titles: list[str]) -> ModuleType:
    quartz = cast(Any, ModuleType("Quartz"))
    quartz.kCGNullWindowID = 0
    quartz.kCGWindowListExcludeDesktopElements = 1
    quartz.kCGWindowListOptionOnScreenOnly = 2
    quartz.CGWindowListCopyWindowInfo = lambda *_a: [{"kCGWindowName": title} for title in titles]
    return cast(ModuleType, quartz)


def test_window_titles_visible_reads_kcg_window_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "Quartz", _fake_quartz(["TST Desk"]))
    assert darwin._window_titles_visible() is True


def test_window_titles_blanked_means_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "Quartz", _fake_quartz(["", "  "]))
    assert darwin._window_titles_visible() is False


def test_capture_uses_coregraphics_and_skips_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\ncg"
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: True)
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: True)
    monkeypatch.setattr(darwin, "_cg_capture_png", lambda _rect: png)

    def fail_cli(*_a: object, **_k: object) -> None:
        raise AssertionError("CLI must not run")

    monkeypatch.setattr(DarwinBackend, "_run_screencapture", fail_cli)
    assert DarwinBackend().capture_png((0, 0, 10, 10)) == png


def test_checkout_capture_uses_host_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\nagent"
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: False)
    monkeypatch.setattr(darwin, "_capture_via_agent", lambda _rect: png)

    def fail_cg(_rect: object) -> None:
        raise AssertionError("checkout Python must not CG-capture")

    monkeypatch.setattr(darwin, "_cg_capture_png", fail_cg)
    assert DarwinBackend().capture_png((0, 0, 10, 10)) == png


def test_checkout_permissions_use_host_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: False)
    monkeypatch.setattr(
        darwin,
        "_permissions_via_agent",
        lambda **_k: {
            "platform": "macos",
            "screen_recording": {"granted": True, "required_for": "x"},
            "accessibility": {"granted": True, "required_for": "y"},
            "all_granted": True,
        },
    )
    report = DarwinBackend().check_permissions(request=True)
    assert report["all_granted"] is True


def test_checkout_permissions_request_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: False)
    seen: list[bool] = []

    def agent(*, request: bool = False) -> dict[str, object]:
        seen.append(request)
        return {
            "platform": "macos",
            "screen_recording": {"granted": True, "required_for": "x"},
            "accessibility": {"granted": True, "required_for": "y"},
            "all_granted": True,
            "actuation_path": "host",
        }

    monkeypatch.setattr(darwin, "_permissions_via_agent", agent)
    report = DarwinBackend().check_permissions(request=True)
    assert seen == [True]
    assert report["actuation_path"] == "host"


def test_capture_does_not_call_screencapture_without_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: False)
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: False)
    monkeypatch.setattr(darwin, "_capture_via_agent", lambda _rect: None)

    def fail_cg(_rect: object) -> None:
        raise AssertionError("CG capture prompts TCC from checkout Python")

    def fail_cli(*_a: object, **_k: object) -> None:
        raise AssertionError("screencapture must not run; it re-prompts TCC")

    monkeypatch.setattr(darwin, "_cg_capture_png", fail_cg)
    monkeypatch.setattr(DarwinBackend, "_run_screencapture", fail_cli)
    with pytest.raises(RuntimeError, match="Do not click Allow"):
        DarwinBackend().capture_png((0, 0, 10, 10))


def test_request_true_does_not_prompt_from_checkout_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: False)
    monkeypatch.setattr(darwin, "_window_titles_visible", lambda: False)
    monkeypatch.setattr(darwin, "_ax_process_trusted", lambda: False)
    monkeypatch.setattr(darwin, "_ax_api_usable", lambda: False)
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: False)

    def fail_screen() -> bool:
        raise AssertionError("checkout Python must not CGRequest")

    def fail_access() -> bool:
        raise AssertionError("checkout Python must not AX prompt")

    monkeypatch.setattr(darwin, "_request_screen_recording", fail_screen)
    monkeypatch.setattr(darwin, "_request_accessibility", fail_access)
    DarwinBackend().check_permissions(request=True)


def test_capture_falls_back_to_cli_when_this_process_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = b"\x89PNG\r\n\x1a\ncli"
    monkeypatch.setattr(darwin, "_is_host_identity", lambda: True)
    monkeypatch.setattr(darwin, "_cg_capture_png", lambda _rect: None)
    monkeypatch.setattr(darwin, "_cg_preflight", lambda: True)
    monkeypatch.setattr(DarwinBackend, "_capture_via_screencapture", lambda _self, _rect: png)
    assert DarwinBackend().capture_png((0, 0, 10, 10)) == png


def test_window_titles_accepts_non_dict_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    # CGWindowListCopyWindowInfo returns NSDictionary, which is not isinstance dict.
    class NsDict:
        def get(self, key: str, default: object = None) -> object:
            return "Finder" if key == "kCGWindowName" else default

    quartz = cast(Any, ModuleType("Quartz"))
    quartz.kCGNullWindowID = 0
    quartz.kCGWindowListExcludeDesktopElements = 1
    quartz.kCGWindowListOptionOnScreenOnly = 2
    quartz.CGWindowListCopyWindowInfo = lambda *_a: [NsDict()]
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    assert darwin._window_titles_visible() is True
