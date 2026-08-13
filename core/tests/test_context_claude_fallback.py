"""Tests for CLAUDE.md fallback during steering discovery (TD-502).

Covers: fallback at every level, shadowing when both files exist,
a CLAUDE.md-only workspace, precedence inheritance, and skip-dir
exclusion.

Uses the same fixture pattern as test_context.py.
"""

from __future__ import annotations

from pathlib import Path

from tstd.context import Precedence, SteeringFileResolver

# ── Helpers ──────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_workspace(
    base: Path,
    *,
    global_agents: str | None = None,
    global_claude: str | None = None,
    root_agents: str | None = None,
    root_claude: str | None = None,
    rules: dict[str, str] | None = None,
    nested: dict[str, tuple[str | None, str | None]] | None = None,
) -> tuple[Path, Path]:
    """Build a workspace in *base* and return ``(home_dir, workspace)``.

    *nested* maps a relative directory path (e.g. ``"src"``) to a tuple
    of ``(agents_content, claude_content)`` where either may be None.
    """
    home = base / "home"
    workspace = base / "workspace"

    if global_agents is not None:
        _write(home / ".tstdesk" / "AGENTS.md", global_agents)
    if global_claude is not None:
        _write(home / ".claude" / "CLAUDE.md", global_claude)
    if root_agents is not None:
        _write(workspace / "AGENTS.md", root_agents)
    if root_claude is not None:
        _write(workspace / "CLAUDE.md", root_claude)
    if rules:
        for name, content in rules.items():
            _write(workspace / ".tst" / "rules" / name, content)
    if nested:
        for rel_path, (agents_content, claude_content) in nested.items():
            if agents_content is not None:
                _write(workspace / rel_path / "AGENTS.md", agents_content)
            if claude_content is not None:
                _write(workspace / rel_path / "CLAUDE.md", claude_content)

    return home, workspace


def _make_resolver(home: Path) -> SteeringFileResolver:
    return SteeringFileResolver(home_dir=home)


# ── Tests: fallback at each level ─────────────────────────────────────────


class TestFallbackAtEachLevel:
    """CLAUDE.md is used when AGENTS.md is absent (criterion 1)."""

    def test_global_fallback_to_claude(self, tmp_path: Path) -> None:
        """~/.claude/CLAUDE.md is used when ~/.tstdesk/AGENTS.md is absent."""
        home, ws = _build_workspace(tmp_path, global_claude="global claude prefs")
        sources = _make_resolver(home).resolve(ws)
        global_sources = [s for s in sources if s.precedence == Precedence.USER_GLOBAL]
        assert len(global_sources) == 1
        assert global_sources[0].is_fallback is True
        assert global_sources[0].path.name == "CLAUDE.md"

    def test_workspace_fallback_to_claude(self, tmp_path: Path) -> None:
        """Workspace CLAUDE.md is used when AGENTS.md is absent."""
        home, ws = _build_workspace(tmp_path, root_claude="workspace claude rules")
        sources = _make_resolver(home).resolve(ws)
        ws_sources = [s for s in sources if s.precedence == Precedence.WORKSPACE]
        assert len(ws_sources) == 1
        assert ws_sources[0].is_fallback is True
        assert ws_sources[0].path.name == "CLAUDE.md"

    def test_nested_fallback_to_claude(self, tmp_path: Path) -> None:
        """Nested CLAUDE.md is used when AGENTS.md is absent."""
        home, ws = _build_workspace(
            tmp_path,
            nested={"src": (None, "src claude rules")},
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 1
        assert nested[0].is_fallback is True
        assert nested[0].path.name == "CLAUDE.md"
        assert nested[0].subtree == "src"

    def test_global_fallback_path_is_correct(self, tmp_path: Path) -> None:
        """Verify the global fallback path is ~/.claude/CLAUDE.md."""
        home, ws = _build_workspace(tmp_path, global_claude="prefs")
        sources = _make_resolver(home).resolve(ws)
        global_sources = [s for s in sources if s.precedence == Precedence.USER_GLOBAL]
        assert len(global_sources) == 1
        expected = home / ".claude" / "CLAUDE.md"
        assert global_sources[0].path == expected


# ── Tests: shadowing ──────────────────────────────────────────────────────


class TestShadowing:
    """When both files exist, AGENTS.md wins and shadowing is recorded (criterion 2)."""

    def test_global_shadowing(self, tmp_path: Path) -> None:
        """Global AGENTS.md shadows ~/.claude/CLAUDE.md."""
        home, ws = _build_workspace(
            tmp_path,
            global_agents="global agents",
            global_claude="global claude",
        )
        sources = _make_resolver(home).resolve(ws)
        global_sources = [s for s in sources if s.precedence == Precedence.USER_GLOBAL]
        assert len(global_sources) == 1
        assert global_sources[0].path.name == "AGENTS.md"
        assert global_sources[0].is_fallback is False
        assert global_sources[0].shadowed_path is not None
        assert global_sources[0].shadowed_path.name == "CLAUDE.md"

    def test_workspace_shadowing(self, tmp_path: Path) -> None:
        """Workspace AGENTS.md shadows CLAUDE.md."""
        home, ws = _build_workspace(
            tmp_path,
            root_agents="agents rules",
            root_claude="claude rules",
        )
        sources = _make_resolver(home).resolve(ws)
        ws_sources = [s for s in sources if s.precedence == Precedence.WORKSPACE]
        assert len(ws_sources) == 1
        assert ws_sources[0].path.name == "AGENTS.md"
        assert ws_sources[0].shadowed_path is not None
        assert ws_sources[0].shadowed_path.name == "CLAUDE.md"

    def test_nested_shadowing(self, tmp_path: Path) -> None:
        """Nested AGENTS.md shadows CLAUDE.md."""
        home, ws = _build_workspace(
            tmp_path,
            nested={"src": ("agents content", "claude content")},
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 1
        assert nested[0].path.name == "AGENTS.md"
        assert nested[0].shadowed_path is not None
        assert nested[0].shadowed_path.name == "CLAUDE.md"

    def test_no_shadowing_when_only_agents(self, tmp_path: Path) -> None:
        """No shadowing when CLAUDE.md is absent."""
        home, ws = _build_workspace(tmp_path, root_agents="agents only")
        sources = _make_resolver(home).resolve(ws)
        ws_sources = [s for s in sources if s.precedence == Precedence.WORKSPACE]
        assert len(ws_sources) == 1
        assert ws_sources[0].shadowed_path is None


# ── Tests: CLAUDE.md-only workspace ───────────────────────────────────────


class TestClaudeOnlyWorkspace:
    """A workspace with only CLAUDE.md files loads with full fidelity (criterion 4)."""

    def test_claude_only_at_all_levels(self, tmp_path: Path) -> None:
        """Global + workspace + nested, all CLAUDE.md, no AGENTS.md anywhere."""
        home, ws = _build_workspace(
            tmp_path,
            global_claude="global claude",
            root_claude="workspace claude",
            rules={"lint.md": "lint rules"},
            nested={"src": (None, "src claude")},
        )
        sources = _make_resolver(home).resolve(ws)

        # All four levels present
        assert len(sources) == 4
        prec_vals = [s.precedence for s in sources]
        assert prec_vals == [
            Precedence.USER_GLOBAL,
            Precedence.WORKSPACE,
            Precedence.RULES,
            Precedence.NESTED,
        ]

        # Fallback flags set on the CLAUDE.md sources, not on the rule
        for s in sources:
            if s.precedence == Precedence.RULES:
                assert s.is_fallback is False
            else:
                assert s.is_fallback is True, f"{s.path.name} should be fallback"

        # No shadowing anywhere
        assert all(s.shadowed_path is None for s in sources)

    def test_claude_only_ordering(self, tmp_path: Path) -> None:
        """CLAUDE.md fallbacks observe precedence: workspace outranks global."""
        home, ws = _build_workspace(
            tmp_path,
            global_claude="global: use tabs",
            root_claude="workspace: use spaces",
        )
        sources = _make_resolver(home).resolve(ws)
        assert len(sources) == 2
        # Last source (highest precedence) wins
        assert sources[-1].path.name == "CLAUDE.md"
        assert sources[-1].precedence == Precedence.WORKSPACE

    def test_claude_only_with_no_global(self, tmp_path: Path) -> None:
        """Workspace CLAUDE.md + nested CLAUDE.md, no global file."""
        home, ws = _build_workspace(
            tmp_path,
            root_claude="workspace",
            nested={"deep": (None, "deep rules")},
        )
        sources = _make_resolver(home).resolve(ws)
        assert len(sources) == 2
        assert sources[0].precedence == Precedence.WORKSPACE
        assert sources[1].precedence == Precedence.NESTED


# ── Tests: fallback precedence inheritance ────────────────────────────────


class TestFallbackPrecedence:
    """Fallback files inherit their level's precedence; ordering unchanged."""

    def test_claude_fallback_still_outranks_lower_level(self, tmp_path: Path) -> None:
        """Workspace-root CLAUDE.md still outranks global AGENTS.md."""
        home, ws = _build_workspace(
            tmp_path,
            global_agents="global",
            root_claude="workspace claude",
        )
        sources = _make_resolver(home).resolve(ws)
        # USER_GLOBAL then WORKSPACE — order preserved
        prec_vals = [s.precedence for s in sources]
        assert prec_vals == [Precedence.USER_GLOBAL, Precedence.WORKSPACE]

    def test_global_claude_still_outranks_nothing(self, tmp_path: Path) -> None:
        """Global CLAUDE.md is still USER_GLOBAL precedence."""
        home, ws = _build_workspace(tmp_path, global_claude="global claude")
        sources = _make_resolver(home).resolve(ws)
        assert sources[0].precedence == Precedence.USER_GLOBAL

    def test_fallback_does_not_create_new_precedence_level(self, tmp_path: Path) -> None:
        """Fallback CLAUDE.md uses the same precedence as AGENTS.md would."""
        home, ws = _build_workspace(tmp_path, root_claude="claude")
        sources = _make_resolver(home).resolve(ws)
        ws_source = sources[0]
        assert ws_source.precedence == Precedence.WORKSPACE


# ── Tests: skip dirs ──────────────────────────────────────────────────────


class TestFallbackSkipDirs:
    """CLAUDE.md inside skip dirs is not discovered as nested."""

    def test_skips_claude_in_dotgit(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            nested={".git": (None, "should not appear")},
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 0

    def test_skips_claude_in_dottst(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            nested={".tst": (None, "should not appear")},
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 0

    def test_skips_claude_in_dotclaude(self, tmp_path: Path) -> None:
        """CLAUDE.md inside .claude/ is not discovered as nested."""
        home, ws = _build_workspace(
            tmp_path,
            nested={".claude": (None, "should not appear")},
        )
        sources = _make_resolver(home).resolve(ws)
        nested = [s for s in sources if s.precedence == Precedence.NESTED]
        assert len(nested) == 0


# ── Tests: assembler propagation ──────────────────────────────────────────


class TestAssemblerPropagation:
    """Assembler propagates fallback and shadowing from SteeringSource to ResolvedSource."""

    def test_fallback_provenance_label(self, tmp_path: Path) -> None:
        """Fallback file gets a 'claude fallback' label in the provenance comment."""
        from tstd.context import ContextAssembler

        home, ws = _build_workspace(tmp_path, root_claude="claude content")
        result = ContextAssembler(resolver=_make_resolver(home)).assemble_sync(ws)
        assert len(result.sources) == 1
        assert result.sources[0].is_fallback is True
        assert "claude fallback" in result.block
        assert result.sources[0].path.name == "CLAUDE.md"

    def test_agents_provenance_no_label(self, tmp_path: Path) -> None:
        """AGENTS.md file does not get a fallback label."""
        from tstd.context import ContextAssembler

        home, ws = _build_workspace(tmp_path, root_agents="agents content")
        result = ContextAssembler(resolver=_make_resolver(home)).assemble_sync(ws)
        assert len(result.sources) == 1
        assert result.sources[0].is_fallback is False
        assert "claude fallback" not in result.block

    def test_shadowed_path_is_propagated(self, tmp_path: Path) -> None:
        """Shadowed path preserved through assembly."""
        from tstd.context import ContextAssembler

        home, ws = _build_workspace(
            tmp_path,
            root_agents="agents",
            root_claude="claude",
        )
        result = ContextAssembler(resolver=_make_resolver(home)).assemble_sync(ws)
        assert len(result.sources) == 1
        assert result.sources[0].shadowed_path is not None
        assert result.sources[0].shadowed_path.name == "CLAUDE.md"

    def test_assembler_block_contains_only_agents_content(self, tmp_path: Path) -> None:
        """When both files exist, only AGENTS.md content appears in the block."""
        from tstd.context import ContextAssembler

        home, ws = _build_workspace(
            tmp_path,
            root_agents="agents content",
            root_claude="claude content",
        )
        result = ContextAssembler(resolver=_make_resolver(home)).assemble_sync(ws)
        assert "agents content" in result.block
        assert "claude content" not in result.block


# ── Tests: edge cases ─────────────────────────────────────────────────────


class TestEdgeCases:
    def test_no_agents_no_claude_returns_empty(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path)
        sources = _make_resolver(home).resolve(ws)
        assert len(sources) == 0

    def test_partial_agents_present(self, tmp_path: Path) -> None:
        """AGENTS.md at one level, CLAUDE.md at another — both used."""
        home, ws = _build_workspace(
            tmp_path,
            root_agents="workspace agents",
            nested={"src": (None, "src claude")},
        )
        sources = _make_resolver(home).resolve(ws)
        assert len(sources) == 2
        assert sources[0].path.name == "AGENTS.md"  # workspace root
        assert sources[0].is_fallback is False
        assert sources[1].path.name == "CLAUDE.md"  # nested fallback
        assert sources[1].is_fallback is True
