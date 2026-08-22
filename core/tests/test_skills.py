"""Skill discovery (TD-4502): sources, precedence, containment, budget."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tstd.skills import (
    SKILL_FILE,
    SKILL_MAX_TOKENS,
    SKILL_SOFT_LINE_LIMIT,
    Skill,
    discover_skills,
    find_skill,
    render_catalog,
    skill_budget_refusal,
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def plant(root: Path, relpath: str, text: str) -> Path:
    """Sync helper: write a skill file (ASYNC240 keeps Path out of async)."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestWorkspaceDiscovery:
    def test_workspace_skill_is_found(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/deploy/{SKILL_FILE}", "Deploy the thing\n")
        found = discover_skills(ws, home=home)
        assert [s.name for s in found] == ["deploy"]
        assert found[0].source == "workspace"
        assert found[0].body == "Deploy the thing\n"
        assert found[0].line_count == 1

    def test_frontmatter_fields_are_parsed(self, ws: Path, home: Path) -> None:
        plant(
            ws,
            f".tst/skills/deploy/{SKILL_FILE}",
            "---\ndescription: Ship to staging\nwhenToUse: releasing any service\n---\nBody.\n",
        )
        (found,) = discover_skills(ws, home=home)
        assert found.description == "Ship to staging"
        assert found.when_to_use == "releasing any service"
        assert found.body == "Body.\n"

    def test_frontmatter_without_when_to_use(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/x/{SKILL_FILE}", "---\ndescription: Just this\n---\nbody\n")
        (found,) = discover_skills(ws, home=home)
        assert found.description == "Just this"
        assert found.when_to_use is None

    def test_blank_metadata_is_none(self, ws: Path, home: Path) -> None:
        plant(
            ws,
            f".tst/skills/x/{SKILL_FILE}",
            "---\ndescription: '  '\nwhenToUse: ''\n---\nbody\n",
        )
        (found,) = discover_skills(ws, home=home)
        assert found.description is None
        assert found.when_to_use is None

    def test_extra_frontmatter_keys_are_not_echoed(self, ws: Path, home: Path) -> None:
        plant(
            ws,
            f".tst/skills/deploy/{SKILL_FILE}",
            "---\ndescription: Ship it\nversion: 2\nallowed-tools: all\n---\nb\n",
        )
        catalog = render_catalog(discover_skills(ws, home=home))
        assert catalog is not None
        assert "Ship it" in catalog
        assert "version" not in catalog
        assert "allowed-tools" not in catalog

    def test_non_scalar_description_degrades_to_none(self, ws: Path, home: Path) -> None:
        plant(
            ws,
            f".tst/skills/deploy/{SKILL_FILE}",
            "---\ndescription:\n  - one\n  - two\n---\nb\n",
        )
        (found,) = discover_skills(ws, home=home)
        assert found.description is None

    def test_results_are_sorted_by_name(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/zulu/{SKILL_FILE}", "z\n")
        plant(ws, f".tst/skills/alpha/{SKILL_FILE}", "a\n")
        assert [s.name for s in discover_skills(ws, home=home)] == ["alpha", "zulu"]

    def test_only_one_level_of_skill_dirs(self, ws: Path, home: Path) -> None:
        # A nested skills tree is invisible, like .tst/rules/ non-recursion.
        plant(ws, f".tst/skills/nested/inner/{SKILL_FILE}", "d\n")
        # A bare file inside the skills root is not a skill dir.
        plant(ws, f".tst/skills/{SKILL_FILE}", "loose\n")
        assert discover_skills(ws, home=home) == []

    def test_dir_without_skill_file_is_skipped(self, ws: Path, home: Path) -> None:
        plant(ws, ".tst/skills/deploy/README.md", "not a skill\n")
        assert discover_skills(ws, home=home) == []


class TestPrecedence:
    def test_user_global_wins_on_collision(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/deploy/{SKILL_FILE}", "workspace body\n")
        plant(home, f".tstdesk/skills/deploy/{SKILL_FILE}", "user body\n")
        (found,) = discover_skills(ws, home=home)
        assert found.source == "user"
        assert found.body == "user body\n"

    def test_workspace_only_names_survive_the_collision(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/deploy/{SKILL_FILE}", "w\n")
        plant(ws, f".tst/skills/keep/{SKILL_FILE}", "k\n")
        plant(home, f".tstdesk/skills/deploy/{SKILL_FILE}", "u\n")
        found = {s.name: s for s in discover_skills(ws, home=home)}
        assert found["keep"].source == "workspace"
        assert found["deploy"].source == "user"

    def test_claude_fallback_when_root_missing(self, ws: Path, home: Path) -> None:
        plant(ws, f".claude/skills/review/{SKILL_FILE}", "review body\n")
        (found,) = discover_skills(ws, home=home)
        assert found.source == "workspace_fallback"

    def test_claude_fallback_when_root_empty(self, ws: Path, home: Path) -> None:
        (ws / ".tst" / "skills").mkdir(parents=True)
        plant(ws, f".claude/skills/review/{SKILL_FILE}", "review body\n")
        (found,) = discover_skills(ws, home=home)
        assert found.source == "workspace_fallback"

    def test_own_dir_presence_suppresses_fallback(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/own/{SKILL_FILE}", "own\n")
        plant(ws, f".claude/skills/shadowed/{SKILL_FILE}", "shadow\n")
        assert [s.name for s in discover_skills(ws, home=home)] == ["own"]

    def test_user_claude_fallback(self, ws: Path, home: Path) -> None:
        plant(home, f".claude/skills/home-review/{SKILL_FILE}", "home review\n")
        (found,) = discover_skills(ws, home=home)
        assert found.source == "user_fallback"

    def test_workspace_fallback_loses_to_user_own(self, ws: Path, home: Path) -> None:
        plant(ws, f".claude/skills/deploy/{SKILL_FILE}", "ws fallback\n")
        plant(home, f".tstdesk/skills/deploy/{SKILL_FILE}", "user own\n")
        (found,) = discover_skills(ws, home=home)
        assert found.source == "user"

    def test_user_fallback_wins_the_fallback_collision(self, ws: Path, home: Path) -> None:
        # Both levels' own roots empty: each falls back to its .claude
        # tree, and a same-name collision still resolves user-first.
        plant(ws, f".claude/skills/shared/{SKILL_FILE}", "workspace copy\n")
        plant(home, f".claude/skills/shared/{SKILL_FILE}", "user copy\n")
        (found,) = discover_skills(ws, home=home)
        assert found.source == "user_fallback"
        assert found.body == "user copy\n"


class TestRobustness:
    def test_missing_dirs_yield_empty(self, ws: Path, home: Path) -> None:
        assert discover_skills(ws, home=home) == []

    def test_home_defaults_to_path_home(self, ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(ws.parent))
        plant(ws.parent, f".tstdesk/skills/homed/{SKILL_FILE}", "from home\n")
        assert [s.name for s in discover_skills(ws)] == ["homed"]

    def test_symlinked_body_escaping_its_dir_is_skipped(self, ws: Path, home: Path) -> None:
        outside = plant(ws.parent, "outside.md", "escaped\n")
        skill_dir = ws / ".tst" / "skills" / "escape"
        skill_dir.mkdir(parents=True)
        os.symlink(outside, skill_dir / SKILL_FILE)
        assert discover_skills(ws, home=home) == []

    def test_symlinked_dir_pointing_outside_is_skipped(self, ws: Path, home: Path) -> None:
        outside = ws.parent / "outside-skill"
        outside.mkdir()
        (outside / SKILL_FILE).write_text("escaped\n", encoding="utf-8")
        skills_root = ws / ".tst" / "skills"
        skills_root.mkdir(parents=True)
        os.symlink(outside, skills_root / "escape")
        assert discover_skills(ws, home=home) == []

    def test_symlinked_skills_root_is_skipped(self, ws: Path, home: Path) -> None:
        # The root itself is the link (TD-4502 review): resolving it first
        # would move the wall, so a symlinked root fails closed.
        vault = ws.parent / "vault"
        vault.mkdir()
        plant(vault, f"secret/{SKILL_FILE}", "---\ndescription: stolen\n---\nSECRET\n")
        (ws / ".tst").mkdir(parents=True)
        os.symlink(vault, ws / ".tst" / "skills")
        assert discover_skills(ws, home=home) == []

    def test_symlinked_user_root_is_skipped_too(self, ws: Path, home: Path) -> None:
        vault = ws.parent / "user-vault"
        vault.mkdir()
        plant(vault, f"secret/{SKILL_FILE}", "SECRET\n")
        (home / ".tstdesk").mkdir(parents=True)
        os.symlink(vault, home / ".tstdesk" / "skills")
        assert discover_skills(ws, home=home) == []

    def test_non_utf8_file_is_skipped(self, ws: Path, home: Path) -> None:
        skill_dir = ws / ".tst" / "skills" / "binary"
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_FILE).write_bytes(b"\xff\xfe\x00bad")
        plant(ws, f".tst/skills/fine/{SKILL_FILE}", "fine\n")
        assert [s.name for s in discover_skills(ws, home=home)] == ["fine"]

    def test_oversized_skill_still_loads(self, ws: Path, home: Path) -> None:
        body = "\n".join(f"line {i}" for i in range(SKILL_SOFT_LINE_LIMIT + 10)) + "\n"
        plant(ws, f".tst/skills/long/{SKILL_FILE}", body)
        (found,) = discover_skills(ws, home=home)
        assert found.line_count == SKILL_SOFT_LINE_LIMIT + 10


class TestBudget:
    def test_small_body_passes(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/small/{SKILL_FILE}", "short body\n")
        (found,) = discover_skills(ws, home=home)
        assert skill_budget_refusal(found) is None

    def test_over_budget_skill_is_refused_not_truncated(self, ws: Path, home: Path) -> None:
        # ~4 chars per heuristic token; clear the cap with room to spare.
        body = "x" * (SKILL_MAX_TOKENS * 4 + 4000)
        plant(ws, f".tst/skills/huge/{SKILL_FILE}", body)
        (found,) = discover_skills(ws, home=home)
        refusal = skill_budget_refusal(found)
        assert refusal is not None
        assert "refused, not truncated" in refusal

    def test_budget_boundary_is_inclusive(self, ws: Path, home: Path) -> None:
        # Heuristic tokens are ceil(chars / 4): exactly the cap loads and
        # one character more refuses — this pins <= at the boundary.
        plant(ws, f".tst/skills/exact/{SKILL_FILE}", "x" * (SKILL_MAX_TOKENS * 4))
        plant(ws, f".tst/skills/over/{SKILL_FILE}", "x" * (SKILL_MAX_TOKENS * 4 + 1))
        found = {s.name: s for s in discover_skills(ws, home=home)}
        assert skill_budget_refusal(found["exact"]) is None
        assert skill_budget_refusal(found["over"]) is not None

    def test_find_skill_exact_name(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/deploy/{SKILL_FILE}", "b\n")
        skills = discover_skills(ws, home=home)
        assert find_skill(skills, "deploy") is skills[0]
        assert find_skill(skills, "Deploy") is None  # exact match only
        assert find_skill(skills, "missing") is None


class TestCatalog:
    def test_empty_catalog_renders_none(self) -> None:
        assert render_catalog([]) is None

    def test_catalog_lists_names_not_bodies(self, ws: Path, home: Path) -> None:
        plant(
            ws,
            f".tst/skills/deploy/{SKILL_FILE}",
            "---\ndescription: Ship it\nwhenToUse: on release\n---\nSECRET-BODY\n",
        )
        (found,) = discover_skills(ws, home=home)
        catalog = render_catalog([found])
        assert catalog is not None
        assert "deploy" in catalog
        assert "Ship it" in catalog
        assert "Use when: on release." in catalog
        assert "SECRET-BODY" not in catalog

    def test_catalog_line_without_metadata(self, ws: Path, home: Path) -> None:
        plant(ws, f".tst/skills/bare/{SKILL_FILE}", "body\n")
        (found,) = discover_skills(ws, home=home)
        catalog = render_catalog([found])
        assert catalog is not None
        assert "- bare:" in catalog

    def test_catalog_line_with_when_to_use_only(self, ws: Path, home: Path) -> None:
        # The AC's named shape: description absent, whenToUse present.
        plant(ws, f".tst/skills/triage/{SKILL_FILE}", "---\nwhenToUse: a bug lands\n---\nb\n")
        (found,) = discover_skills(ws, home=home)
        catalog = render_catalog([found])
        assert catalog is not None
        assert "- triage:" in catalog
        assert "Use when: a bug lands." in catalog

    def test_catalog_mentions_the_loader(self) -> None:
        catalog = render_catalog([Skill(name="n", source="workspace", path=Path("/x"), body="")])
        assert catalog is not None
        assert "load_skill" in catalog
