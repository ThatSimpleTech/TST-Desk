"""Tests for steering file discovery and assembly (TD-501).

Table-driven tests over fixture workspaces covering: none present,
each level alone, all levels together, conflicting rules, deeply nested.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tstd.context import (
    ContextAssembler,
    Precedence,
    SteeringFileResolver,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(
    base: Path,
    *,
    global_file: str | None = None,
    root_file: str | None = None,
    rules: dict[str, str] | None = None,
    nested: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Build a workspace in *base* and return ``(home_dir, workspace)``.

    *home_dir* is a ``.tstdesk`` sibling of the workspace so the
    resolver can find the user-global file.

    Args:
        base: Root tmp_path.
        global_file: Content for ``~/.tstdesk/AGENTS.md``, or None to omit.
        root_file: Content for ``<workspace>/AGENTS.md``, or None.
        rules: Mapping of filename to content for ``.tst/rules/``.
        nested: Mapping of relative path (e.g. ``"src/AGENTS.md"``) to content.
    """
    home = base / "home"
    workspace = base / "workspace"

    if global_file is not None:
        _write(home / ".tstdesk" / "AGENTS.md", global_file)
    if root_file is not None:
        _write(workspace / "AGENTS.md", root_file)
    if rules:
        for name, content in rules.items():
            _write(workspace / ".tst" / "rules" / name, content)
    if nested:
        for rel_path, content in nested.items():
            _write(workspace / rel_path, content)

    return home, workspace


def _make_resolver(home: Path) -> SteeringFileResolver:
    return SteeringFileResolver(home_dir=home)


def _make_assembler(home: Path) -> ContextAssembler:
    return ContextAssembler(resolver=_make_resolver(home))


# ── Tests: precedence order ──────────────────────────────────────────────


class TestPrecedence:
    """Verify that files are discovered in the correct order."""

    def test_every_level_is_included(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            global_file="global prefs",
            root_file="root rules",
            rules={"python.md": "python rules"},
            nested={"src/AGENTS.md": "src rules", "src/api/AGENTS.md": "api rules"},
        )
        resolver = _make_resolver(home)
        sources = resolver.resolve(ws)

        assert len(sources) == 5
        prec = [s.precedence for s in sources]
        assert prec == [
            Precedence.USER_GLOBAL,
            Precedence.WORKSPACE,
            Precedence.RULES,
            Precedence.NESTED,
            Precedence.NESTED,
        ]

    def test_precedence_values_are_ascending(self, tmp_path: Path) -> None:
        """source.precedence must be strictly non-decreasing in the resolved list."""
        home, ws = _build_workspace(
            tmp_path,
            global_file="g",
            root_file="r",
            rules={"a.md": "a"},
            nested={"src/AGENTS.md": "s"},
        )
        sources = _make_resolver(home).resolve(ws)
        vals = [s.precedence.value for s in sources]
        assert vals == sorted(vals), f"precedence not sorted: {vals}"

    def test_rules_are_sorted_alphabetically(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"b-api.md": "b", "a-python.md": "a"},
        )
        sources = _make_resolver(home).resolve(ws)
        rule_sources = [s for s in sources if s.precedence == Precedence.RULES]
        assert len(rule_sources) == 2
        assert rule_sources[0].path.name == "a-python.md"
        assert rule_sources[1].path.name == "b-api.md"

    def test_nested_ordered_shallowest_first(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            nested={
                "src/AGENTS.md": "src",
                "src/api/deep/AGENTS.md": "deep",
                "src/api/AGENTS.md": "api",
            },
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 3
        assert nested[0].subtree == "src"
        assert nested[1].subtree == "src/api"
        assert nested[2].subtree == "src/api/deep"


# ── Tests: subtree scoping ───────────────────────────────────────────────


class TestSubtree:
    """Nested files carry subtree scoping metadata."""

    def test_non_nested_have_no_subtree(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, global_file="g", root_file="r", rules={"x.md": "x"})
        sources = _make_resolver(home).resolve(ws)
        for s in sources:
            if s.precedence in (Precedence.USER_GLOBAL, Precedence.WORKSPACE, Precedence.RULES):
                assert s.subtree is None, f"{s.path.name} should have no subtree"

    def test_nested_have_correct_subtree(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            nested={
                "src/AGENTS.md": "src content",
                "src/api/AGENTS.md": "api content",
            },
        )
        sources = _make_resolver(home).resolve(ws)
        nested = {s.subtree: s for s in sources if s.precedence == Precedence.NESTED}
        assert nested["src"] is not None
        assert nested["src/api"] is not None


# ── Tests: missing files ─────────────────────────────────────────────────


class TestMissingFiles:
    """Missing files are not errors — absent from results."""

    def test_no_steering_files_at_all(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path)
        resolved = _make_resolver(home).resolve(ws)
        assert len(resolved) == 0

    def test_only_global_exists(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, global_file="global")
        resolved = _make_resolver(home).resolve(ws)
        assert len(resolved) == 1
        assert resolved[0].precedence == Precedence.USER_GLOBAL

    def test_only_rules_exist(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, rules={"x.md": "x"})
        resolved = _make_resolver(home).resolve(ws)
        rules = [s for s in resolved if s.precedence == Precedence.RULES]
        assert len(rules) == 1

    def test_only_nested_exists(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, nested={"src/AGENTS.md": "src"})
        resolved = _make_resolver(home).resolve(ws)
        nested = [s for s in resolved if s.precedence == Precedence.NESTED]
        assert len(nested) == 1

    def test_assembler_skips_missing_gracefully(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path)
        result = _make_assembler(home).assemble_sync(ws)
        assert result.block == ""
        assert result.sources == []


# ── Tests: assembly ──────────────────────────────────────────────────────


class TestAssembly:
    """Concatenation, provenance, and the assembled block."""

    def test_concatenates_with_provenance(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            global_file="global content",
            root_file="root content",
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources) == 2

        global_path = home / ".tstdesk" / "AGENTS.md"
        root_path = ws / "AGENTS.md"
        assert f"<!-- from: {global_path}" in result.block
        assert f"<!-- from: {root_path}" in result.block
        assert "global content" in result.block
        assert "root content" in result.block

    def test_provenance_comments_in_order(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            global_file="g",
            root_file="r",
            rules={"z.md": "z"},
            nested={"src/AGENTS.md": "s"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        lines = result.block.split("\n")
        nested_line = next(i for i, line in enumerate(lines) if "nested" in line)
        global_line = next(i for i, line in enumerate(lines) if "user global" in line)
        assert global_line < nested_line

    def test_scope_labels_in_comments(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            global_file="g",
            root_file="r",
            rules={"x.md": "x"},
            nested={"src/AGENTS.md": "s"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "user global" in result.block
        assert "workspace" in result.block
        assert "rules" in result.block
        assert "nested: src" in result.block

    def test_conflicting_rules_highest_precedence_wins(self, tmp_path: Path) -> None:
        """Higher-precedence content appears later in the block."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="root: use tabs",
            rules={"x.md": "rules: use tabs"},
            nested={"src/AGENTS.md": "src: use spaces"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert result.block.rstrip().endswith("src: use spaces")

    def test_all_levels_assembled_together(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            global_file="global prefs",
            root_file="team conventions",
            rules={"lint.md": "lint rules"},
            nested={"lib/AGENTS.md": "lib rules"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources) == 4
        for needle in ("global prefs", "team conventions", "lint rules", "lib rules"):
            assert needle in result.block

    def test_async_assembly_matches_sync(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, global_file="g", root_file="r")
        assembler = _make_assembler(home)
        sync_result = assembler.assemble_sync(ws)
        async_result = asyncio.run(assembler.assemble(ws))
        assert sync_result.block == async_result.block
        assert len(sync_result.sources) == len(async_result.sources)


# ── Tests: skip dirs ─────────────────────────────────────────────────────


class TestSkipDirs:
    """Nested discovery skips VCS and runtime directories."""

    def test_skips_git(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            nested={
                ".git/AGENTS.md": "should not appear",
                "src/AGENTS.md": "src rules",
            },
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 1
        assert nested[0].subtree == "src"

    def test_skips_tst(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            nested={
                ".tst/memory/AGENTS.md": "should not appear",
                "src/AGENTS.md": "src rules",
            },
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 1
        assert nested[0].subtree == "src"


# ── Tests: edge cases ────────────────────────────────────────────────────


class TestEdgeCases:
    def test_workspace_does_not_exist(self, tmp_path: Path) -> None:
        """Non-existent workspace -> empty result (not an error)."""
        home = tmp_path / "home"
        (home / ".tstdesk").mkdir(parents=True, exist_ok=True)
        resolver = _make_resolver(home)
        sources = resolver.resolve(tmp_path / "nonexistent")
        assert len(sources) == 0

    def test_workspace_is_home_directory(self, tmp_path: Path) -> None:
        """Workspace == home dir: global and workspace AGENTS.md are distinct."""
        home = tmp_path / "home"
        _write(home / ".tstdesk" / "AGENTS.md", "global")
        ws = home
        _write(ws / "AGENTS.md", "workspace")
        resolver = _make_resolver(home)
        sources = resolver.resolve(ws)
        assert len(sources) == 2
        assert sources[0].precedence == Precedence.USER_GLOBAL
        assert sources[1].precedence == Precedence.WORKSPACE

    def test_resolved_sources_have_content(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, global_file="hello global")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources) == 1
        assert result.sources[0].content == "hello global"
