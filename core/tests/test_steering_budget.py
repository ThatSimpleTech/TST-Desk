"""Nested steering cannot swamp the prompt (TD-5004).

A session opened on the home folder walked every repo under it and loaded
73 AGENTS.md/CLAUDE.md files (310 KB) into one system prompt, which no
32k or 64k model can take.  These tests pin the three guards: no nested
walk on the home folder, identical nested text loads once, and nested
files past ``NESTED_TOKEN_BUDGET`` are left out with a Doctor notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.context import ContextAssembler, Precedence, SteeringFileResolver
from tstd.context.assembler import (
    NESTED_BUDGET_WARNING,
    NESTED_DUPLICATE_WARNING,
    NESTED_TOKEN_BUDGET,
)
from tstd.context.discover import HOME_NESTED_SKIPPED

DOC = Path(__file__).resolve().parents[2] / "docs" / "steering.md"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _assemble(home: Path, workspace: Path):  # type: ignore[no-untyped-def]
    return ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(workspace)


def test_home_folder_loads_root_but_no_nested_files(home: Path) -> None:
    _write(home / "AGENTS.md", "home root rule\n")
    _write(home / "dev" / "repo" / "AGENTS.md", "repo rule\n")
    _write(home / "dev" / "other" / "CLAUDE.md", "other rule\n")

    result = _assemble(home, home)

    assert [s.precedence for s in result.sources] == [Precedence.WORKSPACE]
    assert "home root rule" in result.block
    assert "repo rule" not in result.block
    assert HOME_NESTED_SKIPPED in result.notices


def test_a_project_below_home_still_walks_its_nested_files(home: Path) -> None:
    project = home / "dev" / "repo"
    _write(project / "AGENTS.md", "root\n")
    _write(project / "src" / "api" / "AGENTS.md", "api rule\n")

    result = _assemble(home, project)

    assert "api rule" in result.block
    assert HOME_NESTED_SKIPPED not in result.notices


def test_identical_nested_copies_load_once(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    contract = "# Contract\nAlways run the tests.\n"
    _write(ws / "AGENTS.md", contract)
    _write(ws / "worktrees" / "a" / "AGENTS.md", contract)
    _write(ws / "worktrees" / "b" / "CLAUDE.md", contract)

    result = _assemble(home, ws)

    assert result.block.count("Always run the tests.") == 1
    copies = [s for s in result.sources if s.precedence == Precedence.NESTED]
    assert len(copies) == 2
    assert all(not s.active and NESTED_DUPLICATE_WARNING in s.warnings for s in copies)


def test_nested_files_past_the_budget_are_left_out_shallowest_kept(
    tmp_path: Path, home: Path
) -> None:
    ws = tmp_path / "ws"
    _write(ws / "AGENTS.md", "root\n")
    # Each file is distinct and about a fifth of the budget, so the
    # budget holds four or five of the twelve.
    chunk = " ".join(f"word{i}" for i in range(NESTED_TOKEN_BUDGET // 5))
    for i in range(12):
        _write(ws / f"pkg{i:02d}" / "AGENTS.md", f"package {i}\n{chunk}\n")

    result = _assemble(home, ws)

    nested = [s for s in result.sources if s.precedence == Precedence.NESTED]
    loaded = [s for s in nested if s.active]
    dropped = [s for s in nested if not s.active]
    assert loaded and dropped
    assert sum(s.token_count.count for s in loaded) <= NESTED_TOKEN_BUDGET
    # Discovery order is shallowest, then subtree name: the kept ones come first.
    assert nested.index(loaded[-1]) < nested.index(dropped[0])
    assert all(NESTED_BUDGET_WARNING in s.warnings for s in dropped)
    notice = next(n for n in result.notices if "over the" in n)
    assert notice.startswith(f"{len(dropped)} nested steering file(s) not loaded")
    assert "pkg11" in notice or "more" in notice


def test_a_left_out_file_raises_no_import_approval(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    outside = tmp_path / "outside.md"
    outside.write_text("external\n", encoding="utf-8")
    _write(ws / "AGENTS.md", "root\n")
    filler = " ".join(f"w{i}" for i in range(NESTED_TOKEN_BUDGET * 2))
    _write(ws / "a" / "AGENTS.md", f"{filler}\n")  # alone it is over budget
    _write(ws / "b" / "AGENTS.md", f"pulls an outside file\n@{outside}\n")
    _write(ws / "c" / "AGENTS.md", f"{filler} again\n")

    result = _assemble(home, ws)

    by_subtree = {s.subtree: s for s in result.sources if s.subtree}
    assert not by_subtree["a"].active
    assert outside in result.pending_imports  # b loads, so its import asks
    assert not by_subtree["c"].active


def test_the_real_shape_of_the_bug_stays_small(tmp_path: Path, home: Path) -> None:
    """Fifty worktree copies of one large contract assemble to one copy."""
    ws = tmp_path / "dev"
    contract = "\n".join(f"rule {i}: be careful" for i in range(400))
    for i in range(50):
        _write(ws / f"repo-wt{i:02d}" / "CLAUDE.md", contract)

    result = _assemble(home, ws)

    assert result.block.count("rule 0: be careful") <= 1


@pytest.mark.parametrize(
    "text", [NESTED_DUPLICATE_WARNING, NESTED_BUDGET_WARNING, HOME_NESTED_SKIPPED]
)
def test_the_guide_quotes_each_message_verbatim(text: str) -> None:
    assert text in DOC.read_text(encoding="utf-8"), f"docs/steering.md does not quote: {text}"
