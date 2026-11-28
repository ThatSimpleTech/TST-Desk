"""A write's approval card shows the change it would make (APPROVE-03).

No tool had the `diff` argument the card read, so an fs_edit card showed an
absolute path and a collapsed JSON blob, and an fs_write card no sign of
whether it created or overwrote a file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tstd.autonomy.classifier import DecisionClass
from tstd.protocol import ApprovalRequest
from tstd.session import Session
from tstd.tools import create_registry
from tstd.tools.diff_preview import PREVIEW_MAX_CHARS, preview_write_diff


def test_a_new_file() -> None:
    diff = preview_write_diff(
        "fs_write", {"path": "notes/new.md", "content": "one\ntwo\n"}, Path("/nowhere")
    )
    assert diff is not None
    assert "+++ b/" in diff and "@@ -0,0 +1,2 @@" in diff
    assert "+one" in diff and "+two" in diff


def test_an_overwrite_shows_what_goes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old line\nkeep\n")
    diff = preview_write_diff(
        "fs_write", {"path": "a.txt", "content": "new line\nkeep\n"}, tmp_path
    )
    assert diff is not None
    assert "-old line" in diff and "+new line" in diff
    assert "a/a.txt" in diff  # workspace-relative, not the absolute path


def test_an_edit_is_applied_to_the_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\ny = 1\n")
    args = {"path": str(tmp_path / "app.py"), "old_string": "1", "new_string": "2"}
    once = preview_write_diff("fs_edit", args, tmp_path)
    assert once is not None and "+x = 2" in once and "+y = 2" not in once
    every = preview_write_diff("fs_edit", {**args, "replace_all": True}, tmp_path)
    assert every is not None and "+x = 2" in every and "+y = 2" in every


def test_what_cannot_be_known_shows_nothing(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    assert (
        preview_write_diff(
            "fs_edit", {"path": "app.py", "old_string": "zz", "new_string": "q"}, tmp_path
        )
        is None
    )
    assert (
        preview_write_diff(
            "fs_edit", {"path": "missing.py", "old_string": "a", "new_string": "b"}, tmp_path
        )
        is None
    )
    assert preview_write_diff("shell", {"command": "ls"}, tmp_path) is None
    assert preview_write_diff("fs_write", {"path": "", "content": "x"}, tmp_path) is None


def test_a_huge_change_is_cut(tmp_path: Path) -> None:
    diff = preview_write_diff("fs_write", {"path": "big.txt", "content": "x\n" * 50_000}, tmp_path)
    assert diff is not None and len(diff) <= PREVIEW_MAX_CHARS + 60
    assert diff.endswith("(the rest of the change is not shown here)")


async def test_the_approval_request_carries_it(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n")
    session = Session(str(tmp_path))
    tool = create_registry().get("fs_edit")
    assert tool is not None
    args = {"path": "hello.txt", "old_string": "hello", "new_string": "goodbye"}
    asked = asyncio.ensure_future(
        session.request_approval("tc-1", tool, args, DecisionClass.B, "Edit hello.txt", "asked")
    )
    for _ in range(50):
        events = [e for e in session.event_log.all_events if isinstance(e, ApprovalRequest)]
        if events:
            break
        await asyncio.sleep(0.01)
    [event] = events
    assert event.diff is not None and "-hello" in event.diff and "+goodbye" in event.diff
    asked.cancel()
