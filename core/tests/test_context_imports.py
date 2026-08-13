"""Tests for import resolution in steering files (TD-504).

Covers: relative/absolute/~ path resolution, depth limits, cycle
detection, code-fence exclusion, inline-code exclusion, missing
files, provenance, and integration with the context assembler.
"""

from __future__ import annotations

from pathlib import Path

from tstd.context import ContextAssembler, SteeringFileResolver

# ── Helpers ──────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(
    base: Path,
    *,
    root_file: str | None = None,
    rules: dict[str, str] | None = None,
    imports: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Build a workspace and return (home_dir, workspace)."""
    home = base / "home"
    workspace = base / "workspace"

    if root_file is not None:
        _write(workspace / "AGENTS.md", root_file)
    if rules:
        for name, content in rules.items():
            _write(workspace / ".tst" / "rules" / name, content)
    if imports:
        for rel_path, content in imports.items():
            _write(workspace / rel_path, content)

    return home, workspace


def _make_assembler(home: Path) -> ContextAssembler:
    return ContextAssembler(resolver=SteeringFileResolver(home_dir=home))


# ── Tests: path resolution ────────────────────────────────────────────────


class TestPathResolution:
    """Relative, absolute, and ~ paths."""

    def test_relative_import(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@docs/arch.md",
            imports={"docs/arch.md": "Architecture notes."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "Architecture notes." in result.block

    def test_absolute_import(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@/does/not/exist.md")
        # This is an absolute path outside the workspace — it won't exist
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 1
        assert "import file not found" in result.import_issues[0]

    def test_tilde_import(self, tmp_path: Path) -> None:
        """~ expands to the resolver's home_dir."""
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@~/global.md")
        _write(home / "global.md", "Global prefs.")
        result = _make_assembler(home).assemble_sync(ws)
        assert "Global prefs." in result.block

    def test_tilde_slash_import(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@~/docs/arch.md")
        _write(home / "docs" / "arch.md", "Archived.")
        result = _make_assembler(home).assemble_sync(ws)
        assert "Archived." in result.block


# ── Tests: depth limit ────────────────────────────────────────────────────


class TestDepthLimit:
    """Max import depth is 4; exceeding it names the chain."""

    def test_depth_4_allowed(self, tmp_path: Path) -> None:
        """4 levels of nested imports succeed."""
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@a.md")
        _write(ws / "a.md", "A.\n@b.md")
        _write(ws / "b.md", "B.\n@c.md")
        _write(ws / "c.md", "C.\n@d.md")
        _write(ws / "d.md", "D.")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 0
        for letter in ("A.", "B.", "C.", "D."):
            assert letter in result.block

    def test_depth_5_errors(self, tmp_path: Path) -> None:
        """5th level of imports produces an error naming the chain."""
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@a.md")
        _write(ws / "a.md", "A.\n@b.md")
        _write(ws / "b.md", "B.\n@c.md")
        _write(ws / "c.md", "C.\n@d.md")
        _write(ws / "d.md", "D.\n@e.md")
        _write(ws / "e.md", "E.")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 1
        assert "max import depth 4 exceeded" in result.import_issues[0]
        # Chain names the path: a.md -> b.md -> c.md -> d.md -> e.md
        assert "a.md" in result.import_issues[0]
        assert "e.md" in result.import_issues[0]
        # D is present, E is not
        assert "D." in result.block
        assert "E." not in result.block

    def test_depth_5_block_contains_error_comment(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@a.md")
        _write(ws / "a.md", "A.\n@b.md")
        _write(ws / "b.md", "B.\n@c.md")
        _write(ws / "c.md", "C.\n@d.md")
        _write(ws / "d.md", "D.\n@e.md")
        _write(ws / "e.md", "E.")
        result = _make_assembler(home).assemble_sync(ws)
        assert "<!-- max import depth 4 exceeded" in result.block
        assert "d.md" in result.block
        assert "E." not in result.block


# ── Tests: cycle detection ────────────────────────────────────────────────


class TestCycleDetection:
    """Cycles are detected and reported with the full cycle path."""

    def test_simple_cycle(self, tmp_path: Path) -> None:
        """a.md imports b.md, b.md imports a.md."""
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@a.md")
        _write(ws / "a.md", "A.\n@b.md")
        _write(ws / "b.md", "B.\n@a.md")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 1
        assert "import cycle detected" in result.import_issues[0]
        # The cycle path should name the full cycle
        assert "a.md" in result.import_issues[0]
        assert "b.md" in result.import_issues[0]
        # A and B present, but the cycle at the second @a.md is caught
        assert "A." in result.block
        assert "B." in result.block
        assert "<!-- import cycle detected" in result.block

    def test_self_import(self, tmp_path: Path) -> None:
        """A file importing itself is a cycle."""
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@a.md")
        _write(ws / "a.md", "A.\n@a.md")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 1
        assert "import cycle detected" in result.import_issues[0]
        assert "a.md -> a.md" in result.import_issues[0]

    def test_diamond_import(self, tmp_path: Path) -> None:
        """Diamond imports are allowed — chain-based, not global visited set."""
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@a.md")
        _write(ws / "a.md", "A: from a.\n@b.md\n@c.md")
        _write(ws / "b.md", "B: from b.\n@d.md")
        _write(ws / "c.md", "C: from c.\n@d.md")
        _write(ws / "d.md", "D: from d.")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 0
        # D appears twice (once via b, once via c)
        assert result.block.count("D: from d.") == 2

    def test_cycle_does_not_abort(self, tmp_path: Path) -> None:
        """Cycle interrupts the loop but other content survives."""
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@a.md\nAfter cycle.")
        _write(ws / "a.md", "A.\n@a.md")
        result = _make_assembler(home).assemble_sync(ws)
        assert "Root." in result.block
        assert "After cycle." in result.block
        assert "A." in result.block
        assert "<!-- import cycle detected" in result.block


# ── Tests: code fence exclusion ───────────────────────────────────────────


class TestCodeFenceExclusion:
    """Import directives inside fenced code blocks are not evaluated."""

    def test_fenced_import_not_evaluated(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n```\n@docs/arch.md\n```\nAfter.",
            imports={"docs/arch.md": "SHOULD NOT APPEAR"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "SHOULD NOT APPEAR" not in result.block
        assert "@docs/arch.md" in result.block  # preserved as literal text
        assert "After." in result.block

    def test_fenced_with_language(self, tmp_path: Path) -> None:
        """```python fences also excluded."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Before.\n```python\n@docs/arch.md\n```\nAfter.",
            imports={"docs/arch.md": "SHOULD NOT APPEAR"},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "SHOULD NOT APPEAR" not in result.block
        assert "@docs/arch.md" in result.block

    def test_import_before_and_after_fence(self, tmp_path: Path) -> None:
        """Imports outside fences are still evaluated."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Before.\n@docs/a.md\n```\n@docs/b.md\n```\nAfter.\n@docs/c.md",
            imports={
                "docs/a.md": "Content A",
                "docs/b.md": "SHOULD NOT APPEAR",
                "docs/c.md": "Content C",
            },
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "Content A" in result.block
        assert "SHOULD NOT APPEAR" not in result.block
        assert "Content C" in result.block

    def test_inline_code_span_excluded(self, tmp_path: Path) -> None:
        """Inline code spans are naturally excluded by the line-anchored regex.
        A line like `@docs/arch.md` starts with a backtick, not @."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Use `@docs/arch.md` for docs.\n@docs/real.md",
            imports={
                "docs/arch.md": "SHOULD NOT APPEAR",
                "docs/real.md": "Real content.",
            },
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "SHOULD NOT APPEAR" not in result.block
        assert "Real content." in result.block


# ── Tests: missing files ──────────────────────────────────────────────────


class TestMissingFiles:
    """Missing import files produce a warning and do not abort."""

    def test_missing_import_warns(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@missing.md\nAfter.")
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 1
        assert "import file not found" in result.import_issues[0]
        assert "missing.md" in result.import_issues[0]
        # Session continues — other content intact
        assert "Root." in result.block
        assert "After." in result.block

    def test_missing_import_comment_in_block(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Root.\n@missing.md")
        result = _make_assembler(home).assemble_sync(ws)
        assert "<!-- import file not found" in result.block


# ── Tests: provenance ─────────────────────────────────────────────────────


class TestProvenance:
    """Imported content carries provenance to its own file."""

    def test_imported_content_has_own_provenance(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@docs/arch.md",
            imports={"docs/arch.md": "Architecture notes."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        # The import renders with its own provenance comment
        assert "<!-- from: " in result.block
        # The workspace AGENTS.md has its own provenance
        assert "(workspace)" in result.block
        # The imported file has an (imported) provenance
        assert "(imported)" in result.block
        # The imported file's path appears in the provenance
        assert "docs/arch.md" in result.block

    def test_nested_import_provenance(self, tmp_path: Path) -> None:
        """Nested imports each have their own provenance comment."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@a.md",
            imports={
                "a.md": "A.\n@b.md",
                "b.md": "B.",
            },
        )
        result = _make_assembler(home).assemble_sync(ws)
        # Both a.md and b.md have (imported) provenance
        assert result.block.count("(imported)") == 2


# ── Tests: import tree on ResolvedSource ──────────────────────────────────


class TestImportTree:
    """The import tree is available for the inspector."""

    def test_imports_on_resolved_source(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@docs/arch.md",
            imports={"docs/arch.md": "Architecture notes."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources) == 1
        assert len(result.sources[0].imports) == 1
        assert result.sources[0].imports[0].path.name == "arch.md"
        assert result.sources[0].imports[0].content is not None
        assert result.sources[0].imports[0].issue is None

    def test_import_tree_depth(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@a.md",
            imports={
                "a.md": "A.\n@b.md",
                "b.md": "B.\n@c.md",
                "c.md": "C.",
            },
        )
        result = _make_assembler(home).assemble_sync(ws)
        imp_a = result.sources[0].imports[0]
        assert imp_a.path.name == "a.md"
        assert len(imp_a.imports) == 1
        imp_b = imp_a.imports[0]
        assert imp_b.path.name == "b.md"
        assert len(imp_b.imports) == 1
        imp_c = imp_b.imports[0]
        assert imp_c.path.name == "c.md"
        assert len(imp_c.imports) == 0


# ── Tests: imports from rules and fallbacks ────────────────────────────────


class TestImportSources:
    """Imports work from any source type."""

    def test_import_from_rule_file(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"lint.md": "Lint rules.\n@docs/lint-style.md"},
            imports={".tst/rules/docs/lint-style.md": "Lint style guide."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "Lint rules." in result.block
        assert "Lint style guide." in result.block

    def test_import_from_claude_fallback(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file=None,  # no AGENTS.md
            imports={"docs/extra.md": "Extra notes."},
        )
        _write(ws / "CLAUDE.md", "Claude root.\n@docs/extra.md")
        result = _make_assembler(home).assemble_sync(ws)
        assert "Claude root." in result.block
        assert "Extra notes." in result.block

    def test_import_from_nested_agents(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            imports={"sub/docs/arch.md": "Subtree arch."},
        )
        _write(ws / "sub" / "AGENTS.md", "Subtree rules.\n@docs/arch.md")
        result = _make_assembler(home).assemble_sync(ws)
        assert "Subtree rules." in result.block
        assert "Subtree arch." in result.block


# ── Tests: inactive scoped rule imports ────────────────────────────────────


class TestInactiveRuleImports:
    """Inactive scoped rules skip import processing — no spurious warnings."""

    def test_inactive_rule_imports_not_processed(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"scoped.md": "---\nappliesTo: [src/**]\n---\nScoped.\n@docs/missing.md"},
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"other/file.py"},
        )
        # Rule is inactive — no import processing, no spurious warnings
        assert len(result.import_issues) == 0
        assert "Scoped." not in result.block
        assert result.sources[0].active is False
        assert result.sources[0].imports == ()

    def test_active_rule_imports_processed(self, tmp_path: Path) -> None:
        """Active scoped rules DO process imports."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"scoped.md": "---\nappliesTo: [src/**]\n---\nScoped.\n@docs/shared.md"},
            imports={".tst/rules/docs/shared.md": "Shared content."},
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"src/main.py"},
        )
        assert result.sources[0].active is True
        assert "Scoped." in result.block
        assert "Shared content." in result.block


# ── Tests: integration with assembler ─────────────────────────────────────


class TestAssemblerIntegration:
    """Full assembly with imports from the assembler API."""

    def test_imports_assembled_sync(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@docs/arch.md",
            imports={"docs/arch.md": "Architecture notes."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "Root." in result.block
        assert "Architecture notes." in result.block
        # Provenance for the imported file
        assert "(imported)" in result.block

    async def test_imports_assembled_async(self, tmp_path: Path) -> None:

        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@docs/arch.md",
            imports={"docs/arch.md": "Architecture notes."},
        )
        result = await _make_assembler(home).assemble(ws)
        assert "Root." in result.block
        assert "Architecture notes." in result.block

    def test_async_matches_sync(self, tmp_path: Path) -> None:
        import asyncio

        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@docs/arch.md",
            imports={"docs/arch.md": "Architecture notes."},
        )
        assembler = _make_assembler(home)
        sync_result = assembler.assemble_sync(ws)
        async_result = asyncio.run(assembler.assemble(ws))
        assert sync_result.block == async_result.block
        assert len(sync_result.import_issues) == len(async_result.import_issues)

    def test_import_issues_from_assembler(self, tmp_path: Path) -> None:
        """import_issues flattened on AssembledSteering."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@missing.md\n@also-missing.md",
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.import_issues) == 2
        for issue in result.import_issues:
            assert "import file not found" in issue


# ── Tests: edge cases ─────────────────────────────────────────────────────


class TestEdgeCases:
    def test_no_imports_unchanged(self, tmp_path: Path) -> None:
        """A file without any imports is unchanged."""
        home, ws = _build_workspace(tmp_path, root_file="Just plain text.")
        result = _make_assembler(home).assemble_sync(ws)
        assert "Just plain text." in result.block
        assert result.sources[0].imports == ()

    def test_import_chain_relative_to_importing_file(self, tmp_path: Path) -> None:
        """Imports resolve relative to the importing file's directory."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@sub/a.md",
            imports={
                "sub/a.md": "A.\n@b.md",  # @b.md resolves to sub/b.md
                "sub/b.md": "B.",
            },
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "A." in result.block
        assert "B." in result.block

    def test_import_does_not_reimport_self(self, tmp_path: Path) -> None:
        """A file that imports a different file works fine."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.\n@a.md",
            imports={"a.md": "A.\n@b.md", "b.md": "B."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert "A." in result.block
        assert "B." in result.block
        assert len(result.import_issues) == 0
