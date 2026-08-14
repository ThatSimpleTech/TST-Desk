"""Tests for filesystem read tools (TD-603).

Covers the acceptance criteria: line ranges with numbers, glob listing
respecting ignore rules, binary refusal, truncation with stated totals,
and encoding-error handling — plus dispatch integration through the
boundary guard.
"""

from __future__ import annotations

from pathlib import Path

from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClassifier
from tstd.tools import (
    ToolDispatcher,
    create_registry,
    fs_list,
    fs_read,
    register_builtin_handlers,
)
from tstd.tools.boundary import PathGuard


async def _stub_worker(prompt: str) -> str:
    return "B"


def make_dispatcher(workspace: Path) -> ToolDispatcher:
    """A dispatcher with classifier + path guard + builtin handlers."""
    boundary = Boundary(workspace_root=workspace)
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(boundary),
            call_worker=_stub_worker,
        ),
        path_guard=PathGuard(boundary),
    )
    register_builtin_handlers(dispatcher)
    return dispatcher


# ── fs_read: content, line ranges, numbers ──────────────────────────────


class TestFsRead:
    async def test_reads_content_with_line_numbers(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("one\ntwo\nthree\n")
        out = await fs_read(None, str(f))
        assert out == "1: one\n2: two\n3: three"

    async def test_offset_skips_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\nd\n")
        out = await fs_read(None, str(f), offset=2)
        assert out == "2: b\n3: c\n4: d"

    async def test_limit_caps_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\nd\n")
        out = await fs_read(None, str(f), limit=2)
        assert out == "1: a\n2: b"

    async def test_offset_and_limit_window(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\nb\nc\nd\ne\n")
        out = await fs_read(None, str(f), limit=2, offset=3)
        assert out == "3: c\n4: d"

    async def test_offset_beyond_end_returns_nothing(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a\n")
        out = await fs_read(None, str(f), offset=99)
        assert out == "(no lines in range)"

    async def test_missing_file_reports_error(self, tmp_path: Path) -> None:
        out = await fs_read(None, str(tmp_path / "nope.txt"))
        assert out.startswith("Error: file not found")

    async def test_directory_reports_error(self, tmp_path: Path) -> None:
        out = await fs_read(None, str(tmp_path))
        assert "is a directory" in out

    async def test_binary_file_refused(self, tmp_path: Path) -> None:
        f = tmp_path / "bin.dat"
        f.write_bytes(b"PK\x03\x04\x00\x00\x00\x00nul\x00\x00")
        out = await fs_read(None, str(f))
        assert "binary" in out.lower()
        assert "\x00" not in out  # never dumped

    async def test_encoding_error_handled_without_crash(self, tmp_path: Path) -> None:
        f = tmp_path / "legacy.txt"
        f.write_bytes(b"\xff\xfe\x80\x81 not utf8")
        out = await fs_read(None, str(f))
        assert "not valid UTF-8" in out

    async def test_large_file_truncated_with_totals(self, tmp_path: Path) -> None:
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 2500)) + "\n")
        out = await fs_read(None, str(f))
        body, _, marker = out.rpartition("\n… [")
        assert marker.startswith("truncated: 2499 lines,")
        assert "bytes total" in marker
        assert len(body.splitlines()) == 2000  # the _MAX_READ_LINES cap


# ── fs_list: glob, ignore rules, recursion ──────────────────────────────


class TestFsList:
    def _workspace(self, tmp_path: Path) -> Path:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.py").write_text("b")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.py").write_text("c")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "junk.js").write_text("j")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.cpython-311.pyc").write_text("x")
        return tmp_path

    async def test_lists_immediate_children(self, tmp_path: Path) -> None:
        ws = self._workspace(tmp_path)
        out = await fs_list(None, str(ws))
        lines = out.splitlines()
        assert "a.txt" in lines
        assert "b.py" in lines
        # Ignored directories are never listed and never descended into.
        assert not any("node_modules" in line or "__pycache__" in line for line in lines)

    async def test_glob_pattern_filters(self, tmp_path: Path) -> None:
        ws = self._workspace(tmp_path)
        out = await fs_list(None, str(ws), pattern="*.py")
        assert out.splitlines() == ["b.py"]

    async def test_recursive_lists_subtree(self, tmp_path: Path) -> None:
        ws = self._workspace(tmp_path)
        out = await fs_list(None, str(ws), pattern="*.py", recursive=True)
        assert out.splitlines() == ["b.py", "sub/c.py"]

    async def test_no_matches(self, tmp_path: Path) -> None:
        ws = self._workspace(tmp_path)
        out = await fs_list(None, str(ws), pattern="*.rs")
        assert out == "(no matches)"

    async def test_missing_directory_reports_error(self, tmp_path: Path) -> None:
        out = await fs_list(None, str(tmp_path / "nope"))
        assert out.startswith("Error: directory not found")

    async def test_file_path_reports_error(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("a")
        out = await fs_list(None, str(f))
        assert "not a directory" in out


# ── Dispatch integration with the boundary guard (TD-602) ───────────────


class TestDispatchIntegration:
    async def test_in_workspace_read_dispatched(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("hello")
        dispatcher = make_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "fs_read", {"path": str(f)})
        assert result.status == "success"
        assert result.output == "1: hello"

    async def test_out_of_workspace_read_refused_by_guard(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("secret")
        dispatcher = make_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "fs_read", {"path": str(outside)})
        assert result.status == "error"
        assert result.error_code == "boundary_refusal"
        assert "outside the workspace" in result.output

    async def test_fs_list_registered_and_dispatched(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a")
        dispatcher = make_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "c1", "fs_list", {"path": str(tmp_path), "pattern": "*.py"}
        )
        assert result.status == "success"
        assert result.output == "a.py"

    async def test_unknown_handler_still_clear(self, tmp_path: Path) -> None:
        # shell is registered but its handler lands in TD-605.
        dispatcher = make_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "shell", {"command": "echo hi"})
        assert result.status == "error"
        assert result.error_code == "no_handler"
