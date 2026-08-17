"""Tests for path-scoped rules with frontmatter (TD-503).

Covers: frontmatter parsing, rules without appliesTo always load,
rules with appliesTo load only when matched, glob matching (exact,
*, **, extensions, spaces, unicode, negation), inactive rules in
sources but not block, and the active flag on ResolvedSource.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.context import ContextAssembler, SteeringFileResolver
from tstd.context.assembler import _path_matches_glob

# ── Helpers ──────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Steering files are UTF-8 by contract (the assembler refuses anything
    # else); write explicitly so non-ASCII fixtures don't land as cp1252
    # on Windows.
    path.write_text(content, encoding="utf-8")


def _build_workspace(
    base: Path,
    *,
    rules: dict[str, str] | None = None,
    root_file: str | None = None,
    nested: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Build a workspace in *base* and return ``(home_dir, workspace)``."""
    home = base / "home"
    workspace = base / "workspace"

    if root_file is not None:
        _write(workspace / "AGENTS.md", root_file)
    if rules:
        for name, content in rules.items():
            _write(workspace / ".tst" / "rules" / name, content)
    if nested:
        for rel_path, content in nested.items():
            _write(workspace / rel_path, content)

    return home, workspace


def _make_assembler(home: Path) -> ContextAssembler:
    return ContextAssembler(resolver=SteeringFileResolver(home_dir=home))


# ── Tests: frontmatter parsing ────────────────────────────────────────────


class TestFrontmatterParsing:
    """Frontmatter is correctly parsed from rule files."""

    def test_valid_frontmatter(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "api.md": (
                    "---\nappliesTo:\n  - src/api/**/*.py\n  - tests/api/**\n---\n"
                    "All endpoints return Pydantic models."
                ),
            },
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources) == 1
        assert result.sources[0].applies_to == ("src/api/**/*.py", "tests/api/**")
        assert "engine: pydantic" not in result.block  # frontmatter stripped
        assert "All endpoints return" in result.block

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"general.md": "Always use type hints."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert len(result.sources) == 1
        assert result.sources[0].applies_to is None
        assert result.sources[0].active is True
        assert "Always use type hints" in result.block

    def test_empty_frontmatter(self, tmp_path: Path) -> None:
        """Empty YAML between --- delimiters is treated as no frontmatter."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"empty.md": "---\n---\nBody content."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].applies_to is None
        assert "Body content." in result.block

    def test_invalid_yaml_frontmatter(self, tmp_path: Path) -> None:
        """Invalid YAML degrades gracefully — no applies_to."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"bad.md": "---\nappliesTo: [unclosed\n---\nBody."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].applies_to is None
        assert "Body." in result.block

    def test_non_dict_frontmatter(self, tmp_path: Path) -> None:
        """Frontmatter that is not a mapping is ignored."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"list.md": "---\n- just\n- a list\n---\nBody."},
        )
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].applies_to is None
        assert "Body." in result.block


# ── Tests: rules without appliesTo ────────────────────────────────────────


class TestAlwaysLoad:
    """Rules without appliesTo always load (criterion 2)."""

    def test_no_applies_to_always_active(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"general.md": "Always use type hints."},
        )
        result = _make_assembler(home).assemble_sync(ws, matched_paths=set())
        assert result.sources[0].active is True
        assert "Always use type hints" in result.block

    def test_empty_applies_to_always_active(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"empty.md": "---\nappliesTo: []\n---\nEmpty list."},
        )
        result = _make_assembler(home).assemble_sync(ws, matched_paths=set())
        assert result.sources[0].active is True

    def test_always_active_with_no_matched_paths(self, tmp_path: Path) -> None:
        """No matched_paths provided → always-load rules still load."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"general.md": "Always load."},
        )
        result = _make_assembler(home).assemble_sync(ws, matched_paths=None)
        assert result.sources[0].active is True


# ── Tests: rules with appliesTo ────────────────────────────────────────────


class TestScopedLoading:
    """Rules with appliesTo load only when matched (criterion 3)."""

    def test_matched_path_activates_rule(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "api.md": ("---\nappliesTo: [src/api/**/*.py]\n---\nUse Pydantic models."),
            },
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"src/api/user.py"},
        )
        assert result.sources[0].active is True
        assert "Use Pydantic models." in result.block

    def test_unmatched_path_deactivates_rule(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "api.md": ("---\nappliesTo: [src/api/**/*.py]\n---\nUse Pydantic models."),
            },
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"src/cli/main.py"},
        )
        assert result.sources[0].active is False
        assert "Use Pydantic models." not in result.block

    def test_inactive_rule_still_in_sources(self, tmp_path: Path) -> None:
        """Inactive rules appear in sources but not in the block."""
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "api.md": "---\nappliesTo: [src/api/**]\n---\nAPI rules.",
            },
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"src/cli/main.py"},
        )
        assert len(result.sources) == 1
        assert result.sources[0].active is False
        assert result.block == ""  # no active sources → empty block

    def test_any_match_in_path_set_activates(self, tmp_path: Path) -> None:
        """One matching path in the set is enough to activate."""
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "api.md": "---\nappliesTo: [src/api/**]\n---\nAPI rules.",
            },
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"src/cli/main.py", "src/api/user.py", "README.md"},
        )
        assert result.sources[0].active is True

    def test_mixed_active_and_inactive(self, tmp_path: Path) -> None:
        """Some rules active, some inactive — both in sources."""
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "always.md": "Always load.",
                "api.md": "---\nappliesTo: [src/api/**]\n---\nAPI rules.",
                "test.md": "---\nappliesTo: [tests/**]\n---\nTest rules.",
            },
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"src/api/user.py"},
        )
        assert len(result.sources) == 3
        # always — active
        assert result.sources[0].active is True
        # api — active (matches src/api/user.py)
        assert result.sources[1].active is True
        # test — inactive (no test path in set)
        assert result.sources[2].active is False
        # Block contains always + api, not test
        assert "Always load." in result.block
        assert "API rules." in result.block
        assert "Test rules." not in result.block


# ── Tests: glob matching ───────────────────────────────────────────────────


class TestGlobMatching:
    """Glob patterns match against workspace-relative paths."""

    def test_exact_path(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [src/main.py]\n---\nExact."},
        )
        # Exact match
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/main.py"})
        assert r1.sources[0].active is True
        # Different path
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/other.py"})
        assert r2.sources[0].active is False

    def test_wildcard_star(self, tmp_path: Path) -> None:
        """* matches any non-/ characters in a single segment."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [src/*.py]\n---\nStar."},
        )
        # src/*.py matches src/main.py but not src/api/user.py
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/main.py"})
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/api/user.py"})
        assert r2.sources[0].active is False

    def test_double_star(self, tmp_path: Path) -> None:
        """** matches any number of path segments."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [src/**/*.py]\n---\nDStar."},
        )
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/main.py"})
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/api/user.py"})
        assert r2.sources[0].active is True
        r3 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/api/v1/user.py"})
        assert r3.sources[0].active is True

    def test_extension_pattern(self, tmp_path: Path) -> None:
        """Patterns like *.py match at any depth."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": '---\nappliesTo:\n  - "*.py"\n---\nPy.'},
        )
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"main.py"})
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/api/user.py"})
        assert r2.sources[0].active is True
        r3 = _make_assembler(home).assemble_sync(ws, matched_paths={"README.md"})
        assert r3.sources[0].active is False

    def test_path_with_spaces(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [my project/**]\n---\nSpaces."},
        )
        r1 = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"my project/src/main.py"},
        )
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"other/src/main.py"})
        assert r2.sources[0].active is False

    def test_unicode_in_path(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [résumé/**]\n---\nUnicode."},
        )
        r1 = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"résumé/cover.pdf", "README.md"},
        )
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/main.py"})
        assert r2.sources[0].active is False

    def test_question_mark(self, tmp_path: Path) -> None:
        """? matches a single non-/ character."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": '---\nappliesTo:\n  - "src/?.py"\n---\nQM.'},
        )
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/a.py"})
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/ab.py"})
        assert r2.sources[0].active is False

    def test_character_class(self, tmp_path: Path) -> None:
        """[a-z] matches a single char in range."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": '---\nappliesTo:\n  - "src/[a-z].py"\n---\nCC.'},
        )
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/m.py"})
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/9.py"})
        assert r2.sources[0].active is False

    def test_negated_character_class(self, tmp_path: Path) -> None:
        """[!a-z] matches a single char NOT in range."""
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": '---\nappliesTo:\n  - "src/[!a-z].py"\n---\nNeg.'},
        )
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/9.py"})
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/m.py"})
        assert r2.sources[0].active is False


# ── Tests: non-rule sources unaffected ─────────────────────────────────────


class TestNonRuleSources:
    """Non-rule sources (global, workspace, nested) are unaffected."""

    def test_agents_md_has_no_applies_to(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(tmp_path, root_file="Root rules.")
        result = _make_assembler(home).assemble_sync(ws, matched_paths=set())
        assert result.sources[0].applies_to is None
        assert result.sources[0].active is True

    def test_rules_dir_only_gets_frontmatter_parsed(self, tmp_path: Path) -> None:
        """Nested AGENTS.md files are not parsed for frontmatter."""
        home, ws = _build_workspace(
            tmp_path,
            root_file="Root.",
            rules={"r.md": "---\nappliesTo: [src/**]\n---\nScoped."},
            nested={"src/AGENTS.md": "---\nappliesTo: [x]\n---\nNested."},
        )
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"other/file.py"},
        )
        # 3 sources: root, rule, nested
        assert len(result.sources) == 3
        # Root — no applies_to, always active
        assert result.sources[0].applies_to is None
        assert result.sources[0].active is True
        # Rule — parsed, not matched, inactive
        assert result.sources[1].applies_to == ("src/**",)
        assert result.sources[1].active is False
        # Nested — NOT parsed for frontmatter, always active
        assert result.sources[2].applies_to is None
        assert result.sources[2].active is True

    def test_claude_files_not_parsed_for_frontmatter(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [src/**]\n---\nScoped."},
        )
        # Add a CLAUDE.md fallback at root
        _write(ws / "CLAUDE.md", "---\nappliesTo: [tests/**]\n---\nClaude fallback.")
        result = _make_assembler(home).assemble_sync(
            ws,
            matched_paths={"src/main.py"},
        )
        # CLAUDE.md is included as workspace-level (not rules)
        # It should NOT be parsed for frontmatter
        for s in result.sources:
            if s.path.name == "CLAUDE.md":
                assert s.applies_to is None
                assert s.active is True


# ── Tests: async path ──────────────────────────────────────────────────────


class TestAsyncAssembly:
    """Async assemble passes matched_paths correctly."""

    async def test_async_with_matched_paths(self, tmp_path: Path) -> None:

        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [src/**]\n---\nScoped."},
        )
        assembler = _make_assembler(home)
        result = await assembler.assemble(ws, matched_paths={"src/main.py"})
        assert result.sources[0].active is True
        assert "Scoped." in result.block

        result2 = await assembler.assemble(ws, matched_paths={"tests/main.py"})
        assert result2.sources[0].active is False
        assert "Scoped." not in result2.block

    async def test_async_matches_sync(self, tmp_path: Path) -> None:

        home, ws = _build_workspace(
            tmp_path,
            rules={"r.md": "---\nappliesTo: [src/**]\n---\nScoped."},
        )
        assembler = _make_assembler(home)
        mp = {"src/main.py"}
        sync_result = assembler.assemble_sync(ws, matched_paths=mp)
        async_result = await assembler.assemble(ws, matched_paths=mp)
        assert sync_result.block == async_result.block
        assert len(sync_result.sources) == len(async_result.sources)
        assert sync_result.sources[0].active == async_result.sources[0].active


# ── Tests: edge cases ──────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_matched_paths_no_scoped_rules(self, tmp_path: Path) -> None:
        """Empty matched_paths → no scoped rules match."""
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "a.md": "---\nappliesTo: [src/**]\n---\nScoped.",
                "b.md": "Always load.",
            },
        )
        result = _make_assembler(home).assemble_sync(ws, matched_paths=set())
        assert result.sources[0].active is False  # scoped
        assert result.sources[1].active is True  # always
        assert "Always load." in result.block

    def test_none_matched_paths_all_rules_active(self, tmp_path: Path) -> None:
        """matched_paths=None → no filtering, all rules active."""
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "r.md": "---\nappliesTo: [src/**]\n---\nScoped.",
            },
        )
        result = _make_assembler(home).assemble_sync(ws, matched_paths=None)
        assert result.sources[0].active is True
        assert "Scoped." in result.block

    def test_multiple_patterns_any_match_activates(self, tmp_path: Path) -> None:
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "r.md": "---\nappliesTo: [src/**, tests/**]\n---\nMulti.",
            },
        )
        r1 = _make_assembler(home).assemble_sync(ws, matched_paths={"src/main.py"})
        assert r1.sources[0].active is True
        r2 = _make_assembler(home).assemble_sync(ws, matched_paths={"tests/test_main.py"})
        assert r2.sources[0].active is True
        r3 = _make_assembler(home).assemble_sync(ws, matched_paths={"docs/readme.md"})
        assert r3.sources[0].active is False

    def test_rule_with_frontmatter_and_no_applies_to_key(self, tmp_path: Path) -> None:
        """Frontmatter without appliesTo key is treated as always-load."""
        home, ws = _build_workspace(
            tmp_path,
            rules={
                "r.md": "---\npriority: high\n---\nOther key in frontmatter.",
            },
        )
        result = _make_assembler(home).assemble_sync(ws, matched_paths=set())
        assert result.sources[0].applies_to is None
        assert result.sources[0].active is True


# ── Tests: frontmatter is stripped at every level (TD-510) ─────────────────

#: One steering file per precedence level, each the only file in its
#: fixture so ``sources[0]`` is unambiguous.  A ``~/`` prefix lands in the
#: home directory, everything else under the workspace root.
_EVERY_LEVEL: list[tuple[str, str]] = [
    ("user-global", "~/.tstdesk/AGENTS.md"),
    ("user-global-claude", "~/.claude/CLAUDE.md"),
    ("workspace", "AGENTS.md"),
    ("workspace-claude-fallback", "CLAUDE.md"),
    ("nested", "src/AGENTS.md"),
    ("rule", ".tst/rules/r.md"),
]

#: Frontmatter in the shape an arrival from another tool actually writes:
#: a scoping key we understand plus keys we do not.
_FOREIGN_FRONTMATTER = '---\nappliesTo: ["src/**"]\ndescription: ported from elsewhere\n---\n'


class TestFrontmatterStrippedAtEveryLevel:
    """TD-510: no steering level ships its YAML block to the model."""

    @pytest.mark.parametrize(
        ("level", "relpath"),
        _EVERY_LEVEL,
        ids=[level for level, _ in _EVERY_LEVEL],
    )
    def test_frontmatter_never_reaches_the_block(
        self, level: str, relpath: str, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        body = f"Body for {level}."
        target = home / relpath.removeprefix("~/") if relpath.startswith("~/") else ws / relpath
        _write(target, f"{_FOREIGN_FRONTMATTER}{body}\n")

        # src/main.py keeps the rule-level fixture active, so every level
        # under test actually reaches the block.
        result = _make_assembler(home).assemble_sync(ws, matched_paths={"src/main.py"})

        assert len(result.sources) == 1, f"{relpath} was not discovered exactly once"
        assert body in result.block
        assert "---" not in result.block, "frontmatter delimiters reached the model"
        assert "appliesTo" not in result.block
        assert "description: ported from elsewhere" not in result.block
        assert result.sources[0].content.strip() == body

    def test_applies_to_outside_rules_is_stripped_but_not_honoured(self, tmp_path: Path) -> None:
        """The recorded decision: strip it, never let it scope.

        The touched path matches nothing in ``appliesTo``.  A rule file
        would go inactive here; a workspace file must not.
        """
        home, ws = _build_workspace(tmp_path, root_file=f"{_FOREIGN_FRONTMATTER}Always on.\n")
        result = _make_assembler(home).assemble_sync(ws, matched_paths={"docs/readme.md"})
        assert result.sources[0].applies_to is None
        assert result.sources[0].active is True
        assert "Always on." in result.block

    def test_applies_to_outside_rules_warns(self, tmp_path: Path) -> None:
        """Stripping silently would swap one silent failure for another."""
        home, ws = _build_workspace(tmp_path, root_file=f"{_FOREIGN_FRONTMATTER}Body.\n")
        result = _make_assembler(home).assemble_sync(ws)
        assert any("appliesTo" in w for w in result.sources[0].warnings)

    def test_applies_to_in_a_rule_does_not_warn(self, tmp_path: Path) -> None:
        """Where it works, it is not a mistake."""
        home, ws = _build_workspace(tmp_path, rules={"r.md": f"{_FOREIGN_FRONTMATTER}Body.\n"})
        result = _make_assembler(home).assemble_sync(ws, matched_paths={"src/a.py"})
        assert result.sources[0].warnings == ()

    def test_other_frontmatter_keys_do_not_warn(self, tmp_path: Path) -> None:
        """Only a dropped *scope* is worth a warning, not any stray key."""
        home, ws = _build_workspace(tmp_path, root_file="---\ndescription: hi\n---\nBody.\n")
        result = _make_assembler(home).assemble_sync(ws)
        assert result.sources[0].warnings == ()
        assert "description" not in result.block

    def test_imports_below_frontmatter_still_resolve(self, tmp_path: Path) -> None:
        """Stripping happens before imports, so the first line still counts."""
        home, ws = _build_workspace(tmp_path, root_file="---\ndescription: hi\n---\n@extra.md\n")
        _write(ws / "extra.md", "Imported body.\n")
        result = _make_assembler(home).assemble_sync(ws)
        assert "Imported body." in result.block
        assert "---" not in result.block


# ── Tests: glob translation table (TD-511) ─────────────────────────────────

#: ``(pattern, path, expected)``.  The bare-name rows are the TD-511 case:
#: a name with no ``/`` anchors at the basename, so it never matches a
#: longer filename that merely ends with it.
_GLOB_TABLE: list[tuple[str, str, bool]] = [
    # A bare name is a basename at any depth — never a suffix.
    ("config.py", "config.py", True),
    ("config.py", "pkg/config.py", True),
    ("config.py", "a/b/c/config.py", True),
    ("config.py", "oldconfig.py", False),
    ("config.py", "pkg/oldconfig.py", False),
    ("config.py", "config.pyi", False),
    ("Dockerfile", "Dockerfile", True),
    ("Dockerfile", "infra/Dockerfile", True),
    ("Dockerfile", "MyDockerfile", False),
    ("Dockerfile", "Dockerfile.dev", False),
    # The explicit `**/` spelling means the same thing, equally anchored.
    ("**/config.py", "config.py", True),
    ("**/config.py", "pkg/config.py", True),
    ("**/config.py", "oldconfig.py", False),
    ("**/config.py", "pkg/oldconfig.py", False),
    # `**/` mid-pattern still crosses separators, and still anchors.
    ("src/**/*.py", "src/main.py", True),
    ("src/**/*.py", "src/api/user.py", True),
    ("src/**/*.py", "src/api/v1/user.py", True),
    ("src/**/*.py", "tests/main.py", False),
    ("ui/**/*.svelte", "ui/App.svelte", True),
    ("ui/**/*.svelte", "ui/src/App.svelte", True),
    ("ui/**/*.svelte", "src/App.svelte", False),
    ("src/**/config.py", "src/config.py", True),
    ("src/**/config.py", "src/a/config.py", True),
    ("src/**/config.py", "src/oldconfig.py", False),
    # A trailing `**` takes everything below, but not the directory itself.
    ("src/api/**", "src/api/routes.py", True),
    ("src/api/**", "src/api/v1/routes.py", True),
    ("src/api/**", "src/api", False),
    # Bare `**` is everything.
    ("**", "anything/at/all.py", True),
    ("**", "top.py", True),
    # A single star stops at a separator.
    ("src/*", "src/routes.py", True),
    ("src/*", "src/v1/routes.py", False),
    # A bare extension glob matches at any depth.
    ("*.py", "main.py", True),
    ("*.py", "src/deep/thing.py", True),
    ("*.py", "src/thing.pyi", False),
    # Exact relative paths are unaffected.
    ("src/main.py", "src/main.py", True),
    ("src/main.py", "src/oldmain.py", False),
    # Character classes and `?` keep working alongside the anchor.
    ("src/?.py", "src/a.py", True),
    ("src/?.py", "src/ab.py", False),
    ("[a-z]onfig.py", "config.py", True),
    ("[a-z]onfig.py", "oldconfig.py", False),
]


@pytest.mark.parametrize(("pattern", "path", "expected"), _GLOB_TABLE)
def test_glob_translation(pattern: str, path: str, expected: bool) -> None:
    """TD-511: the translator table, including the suffix case."""
    assert _path_matches_glob(path, pattern) is expected


@pytest.mark.parametrize(
    ("touched", "expected_active"),
    [
        ("config.py", True),
        ("pkg/config.py", True),
        ("oldconfig.py", False),
        ("src/oldconfig.py", False),
    ],
)
def test_bare_name_scope_through_the_assembler(
    touched: str, expected_active: bool, tmp_path: Path
) -> None:
    """TD-511 criterion 1, driven end to end rather than at the translator."""
    home, ws = _build_workspace(
        tmp_path, rules={"c.md": "---\nappliesTo: [config.py]\n---\nConfig rules."}
    )
    result = _make_assembler(home).assemble_sync(ws, matched_paths={touched})
    assert result.sources[0].active is expected_active
