"""Memory is not steering (TD-2105).

``.tst/memory/**`` must never appear in the instruction stack. Frontmatter
in a memory file is not an ``appliesTo`` rule. A workspace with both
``AGENTS.md`` and ``MEMORY.md`` assembles steering from the former only.
"""

from __future__ import annotations

import os
from pathlib import Path

from tstd.context import ContextAssembler, SteeringFileResolver

_GOLDEN = Path(__file__).parent / "golden" / "block_memory_not_steering.md"
_ROOT = "<ROOT>"

_AGENTS = "Prefer small, reviewable commits.\n"
_MEMORY = """\
---
appliesTo: ["**"]
---
SECRET-MEMORY: last month's decision that must not become a rule.
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _to_golden(text: str, base: Path) -> str:
    return text.replace(base.as_posix(), _ROOT).replace(str(base), _ROOT)


class TestMemoryIsNotSteering:
    def test_memory_path_never_in_sources(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "workspace"
        _write(ws / "AGENTS.md", _AGENTS)
        _write(ws / ".tst" / "memory" / "MEMORY.md", _MEMORY)
        _write(ws / ".tst" / "memory" / "decisions.md", "why we did it\n")
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(ws)
        paths = [s.path.as_posix() for s in result.sources]
        assert all("/.tst/memory/" not in p for p in paths)
        assert all(s.path.name != "MEMORY.md" for s in result.sources)
        assert "SECRET-MEMORY" not in result.block
        assert all(s.applies_to is None for s in result.sources)

    def test_memory_frontmatter_is_not_a_rule(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "workspace"
        _write(ws / ".tst" / "memory" / "gotchas.md", _MEMORY)
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(ws)
        assert result.sources == []
        assert result.block == ""

    def test_agents_and_memory_golden(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "workspace"
        _write(ws / "AGENTS.md", _AGENTS)
        _write(ws / ".tst" / "memory" / "MEMORY.md", _MEMORY)
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(ws)
        actual = _to_golden(result.block, tmp_path)
        if os.environ.get("TSTD_UPDATE_GOLDEN"):
            _GOLDEN.write_text(actual, encoding="utf-8")
        expected = _GOLDEN.read_text(encoding="utf-8") if _GOLDEN.exists() else ""
        assert actual == expected, (
            f"golden mismatch vs {_GOLDEN} — set TSTD_UPDATE_GOLDEN=1 to regenerate"
        )
        assert "Prefer small, reviewable commits." in result.block
        assert "SECRET-MEMORY" not in result.block
