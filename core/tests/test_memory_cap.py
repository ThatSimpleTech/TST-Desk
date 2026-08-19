"""Memory file line cap (TD-2103).

The number lives in ``.tst/config.yaml`` (``memory.max_lines``), never as
a literal in the write handler. Tool writes that would exceed the cap, or
replace a file already at it, are refused with copy that says to distill.
``replace_memory_file`` is the distill path and may replace an at-cap file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_security_suite import make_dispatcher
from tstd.boundary_config import (
    DEFAULT_MEMORY_MAX_LINES,
    BoundaryConfig,
    MemorySection,
    load_workspace_boundary,
)
from tstd.memory_store import (
    MemoryCapError,
    check_memory_write,
    count_lines,
    replace_memory_file,
)
from tstd.session import Session
from tstd.tools.write import fs_edit, fs_write


def _lines(n: int) -> str:
    return "".join(f"line {i}\n" for i in range(n))


def _session(ws: Path, max_lines: int) -> Session:
    sess = Session(str(ws))
    sess.boundary_config = BoundaryConfig(memory=MemorySection(max_lines=max_lines))
    return sess


class TestConfig:
    def test_default_is_two_hundred(self) -> None:
        assert DEFAULT_MEMORY_MAX_LINES == 200
        assert BoundaryConfig().memory.max_lines == 200

    def test_config_value_is_what_the_cap_uses(self, tmp_path: Path) -> None:
        (tmp_path / ".tst").mkdir()
        (tmp_path / ".tst" / "config.yaml").write_text(
            "memory:\n  max_lines: 7\n", encoding="utf-8"
        )
        assert load_workspace_boundary(tmp_path).memory.max_lines == 7

    def test_zero_is_a_load_error(self, tmp_path: Path) -> None:
        from tstd.config import ConfigError

        (tmp_path / ".tst").mkdir()
        (tmp_path / ".tst" / "config.yaml").write_text(
            "memory:\n  max_lines: 0\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError) as ei:
            load_workspace_boundary(tmp_path)
        assert "max_lines" in str(ei.value)


class TestCheck:
    def test_under_cap_is_fine(self, tmp_path: Path) -> None:
        path = tmp_path / ".tst" / "memory" / "gotchas.md"
        check_memory_write(path, _lines(3), 5)

    def test_exceed_names_distill_not_append(self, tmp_path: Path) -> None:
        path = tmp_path / "gotchas.md"
        with pytest.raises(MemoryCapError) as ei:
            check_memory_write(path, _lines(6), 5)
        msg = str(ei.value).lower()
        assert "distill" in msg
        assert "do not append" in msg
        assert "5" in str(ei.value)

    def test_at_cap_file_cannot_be_replaced_by_tools(self, tmp_path: Path) -> None:
        path = tmp_path / "MEMORY.md"
        path.write_text(_lines(5), encoding="utf-8")
        with pytest.raises(MemoryCapError) as ei:
            check_memory_write(path, _lines(2), 5)
        assert "already at the 5-line cap" in str(ei.value)
        assert "Distill" in str(ei.value)

    def test_distill_may_replace_at_cap(self, tmp_path: Path) -> None:
        path = tmp_path / "MEMORY.md"
        path.write_text(_lines(5), encoding="utf-8")
        replace_memory_file(path, _lines(2), 5)
        assert count_lines(path.read_text(encoding="utf-8")) == 2

    def test_distill_still_cannot_exceed(self, tmp_path: Path) -> None:
        path = tmp_path / "MEMORY.md"
        path.write_text(_lines(5), encoding="utf-8")
        with pytest.raises(MemoryCapError):
            replace_memory_file(path, _lines(6), 5)
        assert count_lines(path.read_text(encoding="utf-8")) == 5


class TestHandlers:
    async def test_non_memory_write_is_uncapped(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "a.py"
        await fs_write(None, str(target), _lines(500))
        assert count_lines(target.read_text(encoding="utf-8")) == 500

    async def test_memory_write_under_cap_succeeds(self, tmp_path: Path) -> None:
        sess = _session(tmp_path, 8)
        path = tmp_path / ".tst" / "memory" / "gotchas.md"
        await fs_write(sess, str(path), _lines(3))
        assert path.read_text(encoding="utf-8") == _lines(3)

    async def test_memory_write_over_cap_is_refused(self, tmp_path: Path) -> None:
        sess = _session(tmp_path, 4)
        path = tmp_path / ".tst" / "memory" / "gotchas.md"
        with pytest.raises(MemoryCapError) as ei:
            await fs_write(sess, str(path), _lines(5))
        assert "distill" in str(ei.value).lower()
        assert "do not append" in str(ei.value).lower()
        assert not path.exists()

    async def test_append_that_would_exceed_is_refused(self, tmp_path: Path) -> None:
        sess = _session(tmp_path, 4)
        path = tmp_path / ".tst" / "memory" / "decisions.md"
        path.parent.mkdir(parents=True)
        path.write_text(_lines(3), encoding="utf-8")
        with pytest.raises(MemoryCapError):
            await fs_write(sess, str(path), _lines(2), append=True)
        assert count_lines(path.read_text(encoding="utf-8")) == 3

    async def test_edit_that_would_exceed_is_refused(self, tmp_path: Path) -> None:
        sess = _session(tmp_path, 3)
        path = tmp_path / ".tst" / "memory" / "MEMORY.md"
        path.parent.mkdir(parents=True)
        path.write_text("a\nb\nc\n", encoding="utf-8")
        with pytest.raises(MemoryCapError):
            await fs_edit(sess, str(path), "c", "c\nd")
        assert path.read_text(encoding="utf-8") == "a\nb\nc\n"

    async def test_at_cap_file_refuses_overwrite_until_distill(self, tmp_path: Path) -> None:
        sess = _session(tmp_path, 3)
        path = tmp_path / ".tst" / "memory" / "gotchas.md"
        path.parent.mkdir(parents=True)
        path.write_text(_lines(3), encoding="utf-8")
        with pytest.raises(MemoryCapError):
            await fs_write(sess, str(path), _lines(1))
        replace_memory_file(path, _lines(1), 3)
        assert count_lines(path.read_text(encoding="utf-8")) == 1

    def test_handler_has_no_cap_literal(self) -> None:
        source = Path(__file__).resolve().parents[1] / "tstd" / "tools" / "write.py"
        text = source.read_text(encoding="utf-8")
        assert "200" not in text

    async def test_dispatch_surfaces_the_copy(self, tmp_path: Path) -> None:
        sess = _session(tmp_path, 2)
        dispatcher = make_dispatcher(tmp_path)
        path = tmp_path / ".tst" / "memory" / "gotchas.md"
        result = await dispatcher.dispatch(
            "c1",
            "fs_write",
            {"path": str(path), "content": _lines(3)},
            session=sess,
        )
        assert result.status == "error"
        assert result.error_code == "handler_error"
        assert "distill" in result.output.lower()
        assert "do not append" in result.output.lower()
        assert not path.exists()
