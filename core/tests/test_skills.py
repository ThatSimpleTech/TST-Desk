"""SKILL.md skills: discovery, catalog, loading, budget refusal (TD-4502).

Discovery tests use isolated home/workspace trees (the test_commands.py
pattern) so nothing reads a real ``~/.tstdesk``. Handler tests stand in
a session-shaped namespace — load_skill touches workspace_path,
loaded_skills, router, and conversation, and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tstd.compaction import budget_threshold
from tstd.config import ModelConfig, Preset, TierConfig
from tstd.context import ContextAssembler, SteeringFileResolver
from tstd.context.skills import (
    FALLBACK_SKILL_BUDGET_TOKENS,
    LoadedSkill,
    SkillFile,
    build_skills_catalog,
    discover_skills,
    global_skills_dir,
    read_skill_body,
    render_loaded_skill,
)
from tstd.context.stack import build_instruction_stack
from tstd.daemon import Daemon
from tstd.tools.handlers import _remaining_skill_budget, load_skill


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """An isolated home so ~/.tstdesk lookups never see the real one."""
    return tmp_path / "home"


def _skill(root: Path, name: str, text: str = "Do the thing.\n") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sess(ws: Path, **extra: object) -> SimpleNamespace:
    return SimpleNamespace(workspace_path=str(ws), loaded_skills={}, **extra)


def _config(window: int = 100_000) -> ModelConfig:
    def tier() -> TierConfig:
        return TierConfig(
            slug="test-brain",
            base_url="http://mock.local/v1",
            input_price=1.0,
            output_price=2.0,
            cache_read_price=0.5,
            context_window=window,
            max_output_tokens=1_000,
        )

    return ModelConfig(
        presets={"test": Preset(brain=tier(), worker=tier(), validator=tier())},
        active_preset="test",
    )


def _assemble(home: Path, workspace: Path):
    assembler = ContextAssembler(resolver=SteeringFileResolver(home_dir=home))
    return assembler.assemble_sync(workspace)


class TestDiscoverSkills:
    def test_workspace_tree_discovered_with_frontmatter(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(
            ws / ".tst" / "skills",
            "deploy",
            "---\ndescription: ship it\nwhenToUse: before a release\n---\nSteps.\n",
        )
        found = discover_skills(ws, home_dir=home)
        assert [(s.name, s.source, s.fallback) for s in found] == [("deploy", "workspace", False)]
        assert found[0].description == "ship it"
        assert found[0].when_to_use == "before a release"

    def test_user_global_wins_a_shared_name(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "deploy", "workspace body\n")
        _skill(global_skills_dir(home), "deploy", "user body\n")
        found = discover_skills(ws, home_dir=home)
        assert len(found) == 1
        assert (found[0].source, found[0].fallback) == ("user", False)

    def test_claude_fallback_when_ours_empty(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        ours = _skill(ws / ".claude" / "skills", "review")
        theirs = _skill(home / ".claude" / "skills", "audit")
        found = discover_skills(ws, home_dir=home)
        by_name = {s.name: s for s in found}
        assert by_name["review"].fallback is True
        assert by_name["review"].path == ours.resolve()
        assert by_name["audit"].fallback is True
        assert by_name["audit"].path == theirs.resolve()

    def test_fallback_shadowed_once_ours_exist(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "review")
        _skill(ws / ".claude" / "skills", "review", "claude copy\n")
        found = discover_skills(ws, home_dir=home)
        assert len(found) == 1
        assert found[0].fallback is False

    def test_unsafe_names_and_bodyless_dirs_skipped(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        skills = ws / ".tst" / "skills"
        (skills / "my skill").mkdir(parents=True)  # unspellable after /
        (skills / "empty").mkdir(parents=True)  # no SKILL.md inside
        _skill(skills, "real")
        assert [s.name for s in discover_skills(ws, home_dir=home)] == ["real"]

    def test_symlink_escape_skipped(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        outside = tmp_path / "outside"
        _skill(outside, "evil")
        _skill(ws / ".tst" / "skills", "real")
        (ws / ".tst" / "skills" / "sneaky").symlink_to(outside / "evil")
        assert [s.name for s in discover_skills(ws, home_dir=home)] == ["real"]

    def test_missing_everything_is_empty(self, tmp_path: Path, home: Path) -> None:
        assert discover_skills(tmp_path / "nope", home_dir=home) == []


class TestFrontmatterRules:
    def test_unknown_keys_ignored(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(
            ws / ".tst" / "skills",
            "x",
            "---\ndescription: d\nwhenToUse: w\nlicense: MIT\n---\nBody.\n",
        )
        skill = discover_skills(ws, home_dir=home)[0]
        assert (skill.description, skill.when_to_use) == ("d", "w")

    def test_invalid_yaml_reads_as_empty(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "x", "---\ndescription: [unclosed\n---\nBody.\n")
        skill = discover_skills(ws, home_dir=home)[0]
        assert (skill.description, skill.when_to_use) == ("", "")

    def test_non_mapping_yaml_reads_as_empty(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "x", "---\n- just\n- a list\n---\nBody.\n")
        skill = discover_skills(ws, home_dir=home)[0]
        assert (skill.description, skill.when_to_use) == ("", "")

    def test_no_frontmatter_reads_as_empty(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "x", "Plain prose only.\n")
        skill = discover_skills(ws, home_dir=home)[0]
        assert (skill.description, skill.when_to_use) == ("", "")

    def test_undecodable_file_still_listed_metadata_empty(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        broken = ws / ".tst" / "skills" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00bin")
        skill = discover_skills(ws, home_dir=home)[0]
        assert skill.name == "broken"
        assert (skill.description, skill.when_to_use) == ("", "")


class TestBodiesAndRendering:
    def test_read_skill_body_strips_frontmatter(self, tmp_path: Path) -> None:
        path = _skill(tmp_path, "deploy", "---\ndescription: ship it\n---\nRun the deploy.\n")
        assert read_skill_body(path).strip() == "Run the deploy."

    def test_read_skill_body_without_frontmatter(self, tmp_path: Path) -> None:
        path = _skill(tmp_path, "deploy", "Just prose.\n")
        assert read_skill_body(path).strip() == "Just prose."

    def test_catalog_none_when_empty(self) -> None:
        assert build_skills_catalog([]) is None

    def test_catalog_rows_carry_metadata(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "a", "---\ndescription: Alpha\ntags: x\n---\nA.\n")
        _skill(
            ws / ".tst" / "skills",
            "b",
            "---\ndescription: Beta\nwhenToUse: on Fridays\n---\nB.\n",
        )
        catalog = build_skills_catalog(discover_skills(ws, home_dir=home))
        assert catalog is not None
        assert "load_skill" in catalog  # tells the brain how to fetch bodies
        assert "- a: Alpha" in catalog
        assert "- b: Beta (use when: on Fridays)" in catalog
        assert "tags" not in catalog  # unknown frontmatter never renders

    def test_render_loaded_skill_wraps_with_provenance(self, tmp_path: Path) -> None:
        skill = SkillFile(name="deploy", path=tmp_path / "SKILL.md", source="workspace")
        rendered = render_loaded_skill(skill, "Run it.\n")
        assert rendered.startswith("--- skill: deploy (")
        assert rendered.endswith("--- end of skill ---")
        assert "Run it." in rendered

    def test_display_path_tildes_user_files(self, tmp_path, monkeypatch) -> None:
        fake_home = tmp_path / "h"
        monkeypatch.setattr(Path, "home", lambda: fake_home)  # type: ignore[method-assign]
        user = SkillFile(
            name="u", path=fake_home / ".tstdesk" / "skills" / "u" / "SKILL.md", source="user"
        )
        ws = SkillFile(name="w", path=tmp_path / "ws" / "SKILL.md", source="workspace")
        assert user.display_path.startswith("~/")
        assert str(tmp_path) in ws.display_path


class TestLoadSkillTool:
    @pytest.mark.asyncio
    async def test_needs_a_workspace(self) -> None:
        result = await load_skill(SimpleNamespace(loaded_skills={}), "deploy")  # type: ignore[arg-type]
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_happy_path_renders_and_records(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "deploy", "---\ndescription: d\n---\nShip it fast.\n")
        sess = _sess(ws)
        result = await load_skill(sess, "deploy")  # type: ignore[arg-type]
        assert "Ship it fast." in result
        assert "--- end of skill ---" in result
        recorded = sess.loaded_skills["deploy"]
        assert isinstance(recorded, LoadedSkill)
        assert recorded.source == "workspace"
        assert recorded.tokens > 0

    @pytest.mark.asyncio
    async def test_unknown_name_lists_available(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "deploy")
        result = await load_skill(_sess(ws), "nope")  # type: ignore[arg-type]
        assert result.startswith("Error:")
        assert "deploy" in result

    @pytest.mark.asyncio
    async def test_unreadable_body_is_an_error(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        broken = ws / ".tst" / "skills" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00bin")
        result = await load_skill(_sess(ws), "broken")  # type: ignore[arg-type]
        assert result.startswith("Error:")
        assert "unreadable" in result

    @pytest.mark.asyncio
    async def test_over_budget_refused_whole_not_truncated(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        body = "word " * 700  # ~875 estimated tokens
        _skill(ws / ".tst" / "skills", "big", f"---\ndescription: d\n---\n{body}")
        config = _config(window=2_000)  # threshold int((2000-1000)*0.8) = 800
        sess = _sess(ws, conversation=[])
        result = await load_skill(sess, "big", model_config=config)  # type: ignore[arg-type]
        assert "refused rather than truncated" in result
        assert "word word word" not in result  # no partial delivery
        assert sess.loaded_skills == {}

    @pytest.mark.asyncio
    async def test_budget_floor_when_no_config(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "small", "Tiny.\n")
        sess = _sess(ws)
        result = await load_skill(sess, "small")  # type: ignore[arg-type]
        assert "Tiny." in result


class TestBudgetMath:
    def test_floor_without_config(self) -> None:
        assert _remaining_skill_budget(object(), None) == FALLBACK_SKILL_BUDGET_TOKENS

    def test_unknown_tier_falls_back(self) -> None:
        router = SimpleNamespace(active_tier="nope")
        session = SimpleNamespace(router=router)
        assert (
            _remaining_skill_budget(session, _config())  # type: ignore[arg-type]
            == FALLBACK_SKILL_BUDGET_TOKENS
        )

    def test_threshold_minus_conversation(self) -> None:
        session = SimpleNamespace(
            router=SimpleNamespace(active_tier="brain"),
            conversation=[SimpleNamespace(content="x" * 400, tool_calls=None)],
        )
        config = _config(window=100_000)
        expected = budget_threshold(config.tier("brain")) - 100  # ceil(400/4)
        assert _remaining_skill_budget(session, config) == expected  # type: ignore[arg-type]


class TestSlashSkills:
    @pytest.mark.asyncio
    async def test_skill_expands_and_records(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _skill(ws / ".tst" / "skills", "deploy", "---\nhidden: true\n---\nRoll it out.\n")
        daemon = Daemon(data_dir=tmp_path / "data")
        sess = _sess(ws)
        expanded = await daemon._expand_slash(sess, "/deploy prod")  # type: ignore[arg-type]
        assert "Roll it out." in expanded
        assert "prod" in expanded
        assert "hidden" not in expanded
        assert "deploy" in sess.loaded_skills

    @pytest.mark.asyncio
    async def test_command_wins_over_same_named_skill(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        commands = ws / ".tst" / "commands"
        commands.mkdir(parents=True)
        (commands / "deploy.md").write_text("Command body.\n", encoding="utf-8")
        _skill(ws / ".tst" / "skills", "deploy", "Skill body.\n")
        daemon = Daemon(data_dir=tmp_path / "data")
        sess = _sess(ws)
        expanded = await daemon._expand_slash(sess, "/deploy")  # type: ignore[arg-type]
        assert "Command body." in expanded
        assert sess.loaded_skills == {}  # no skill was loaded

    @pytest.mark.asyncio
    async def test_unreadable_skill_delivered_verbatim(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        broken = ws / ".tst" / "skills" / "broken"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_bytes(b"\xff\xfe\x00bin")
        daemon = Daemon(data_dir=tmp_path / "data")
        sess = _sess(ws)
        result = await daemon._expand_slash(sess, "/broken args")  # type: ignore[arg-type]
        assert result == "/broken args"
        assert sess.loaded_skills == {}


class TestStackPassthrough:
    def test_loaded_skills_become_stack_entries(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        loaded = [
            LoadedSkill(
                name="deploy",
                source="workspace",
                path=str(ws / ".tst" / "skills" / "deploy" / "SKILL.md"),
                tokens=42,
            ),
            LoadedSkill(
                name="review",
                source="user",
                path="/home/u/.tstdesk/skills/review/SKILL.md",
                tokens=7,
            ),
        ]
        stack = build_instruction_stack("s1", _assemble(home, ws), loaded_skills=loaded)
        assert [(s.name, s.source, s.tokens) for s in stack.skills] == [
            ("deploy", "workspace", 42),
            ("review", "user", 7),
        ]
        assert all(s.token_method for s in stack.skills)  # method always stated

    def test_no_loads_is_an_empty_list(self, tmp_path: Path, home: Path) -> None:
        ws = tmp_path / "ws"
        stack = build_instruction_stack("s1", _assemble(home, ws))
        assert stack.skills == []
