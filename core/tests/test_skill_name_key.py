"""Claude Code skills load: `name`, the `whenToUse` fallback, ignored keys (TD-5011).

TD-4502 skipped a skill with any key besides ``description`` and
``whenToUse``, so every Claude Code skill (each has ``name`` and none has
``whenToUse``) was dropped.  The directory name still wins over ``name``.
Keys that only label a skill or grant it something are ignored; a key
that would restrict or reroute it still skips the skill.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tstd.context.skills import (
    SkillDiscoverer,
    apply_slash_skill,
    render_skill_catalog,
    try_load_skill,
)
from tstd.session import Session


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _claude_skill(home: Path, directory: str, frontmatter: str, body: str = "Do it.\n") -> Path:
    path = home / ".claude" / "skills" / directory / "SKILL.md"
    _write(path, f"---\n{frontmatter}\n---\n{body}")
    return path


def _names(ws: Path, home: Path) -> list[str]:
    return [s.name for s in SkillDiscoverer(home_dir=home).discover(ws)]


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws, tmp_path / "home"


class TestClaudeFormat:
    def test_name_and_description_only_loads(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_skill(home, "yt-plan", "name: yt-plan\ndescription: Plan a video. Use when asked")
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [s.name for s in skills] == ["yt-plan"]
        assert skills[0].source == "claude_user"
        assert skills[0].description == "Plan a video. Use when asked"
        assert skills[0].when_to_use == skills[0].description

    def test_our_tree_accepts_name_too(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _write(
            ws / ".tst" / "skills" / "review" / "SKILL.md",
            "---\nname: review\ndescription: Review\nwhenToUse: After a diff\n---\nbody\n",
        )
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [(s.name, s.when_to_use) for s in skills] == [("review", "After a diff")]

    def test_folded_description_loads(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_skill(home, "forge", "name: forge\ndescription: >\n  Two lines\n  of text")
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert skills[0].description == "Two lines of text"


class TestNameKey:
    def test_mismatch_keeps_directory_name_and_logs(
        self, dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, home = dirs
        path = _claude_skill(home, "yt-seo", "name: youtube-seo\ndescription: SEO pass")
        with caplog.at_level(logging.WARNING, logger="tstd.skills"):
            assert _names(ws, home) == ["yt-seo"]
        record = next(r for r in caplog.records if "differs from its directory" in r.getMessage())
        fields = record.__dict__["extra_fields"]
        assert fields == {"path": str(path), "name": "youtube-seo"}

    def test_matching_name_does_not_log(
        self, dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, home = dirs
        _claude_skill(home, "yt-seo", "name: yt-seo\ndescription: SEO pass")
        with caplog.at_level(logging.WARNING, logger="tstd.skills"):
            assert _names(ws, home) == ["yt-seo"]
        assert not caplog.records

    @pytest.mark.parametrize("value", ["3", "[a, b]", "{x: 1}"])
    def test_non_string_name_skips(self, dirs: tuple[Path, Path], value: str) -> None:
        ws, home = dirs
        _claude_skill(home, "odd", f"name: {value}\ndescription: Odd")
        assert _names(ws, home) == []

    def test_load_and_slash_use_the_directory_name(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_skill(home, "yt-seo", "name: youtube-seo\ndescription: SEO pass")
        session = Session(str(ws))
        assert try_load_skill(session, "youtube-seo", home_dir=home).startswith("Error:")
        result = apply_slash_skill(session, "/yt-seo now", home_dir=home)
        assert result is not None and result.startswith("Loaded skill `yt-seo`")
        assert session.loaded_skills == ["yt-seo"]


class TestWhenToUse:
    @pytest.mark.parametrize("line", ["", "whenToUse:", "whenToUse: '  '"])
    def test_absent_or_blank_falls_back_to_description(
        self, dirs: tuple[Path, Path], line: str
    ) -> None:
        ws, home = dirs
        _claude_skill(home, "s", f"description: Summarize\n{line}".rstrip())
        skills = SkillDiscoverer(home_dir=home).discover(ws)
        assert [(s.name, s.when_to_use) for s in skills] == [("s", "Summarize")]

    def test_non_string_skips(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_skill(home, "s", "description: Summarize\nwhenToUse: [a, b]")
        assert _names(ws, home) == []

    @pytest.mark.parametrize("line", ["", "description: ''", "description: 7"])
    def test_description_is_still_required(self, dirs: tuple[Path, Path], line: str) -> None:
        ws, home = dirs
        _claude_skill(home, "s", f"name: s\n{line}".rstrip())
        assert _names(ws, home) == []


class TestOtherClaudeKeys:
    @pytest.mark.parametrize(
        "line",
        [
            "license: MIT",
            "version: 1.2.0",
            "metadata: {author: someone, tags: [a]}",
            "compatibility: claude-code",
            "argument-hint: '[topic]'",
            "allowed-tools: Read, Grep, Bash(git:*)",
            "disable-model-invocation: false",
            "user-invocable: true",
        ],
    )
    def test_ignored_key_loads(self, dirs: tuple[Path, Path], line: str) -> None:
        ws, home = dirs
        _claude_skill(home, "s", f"name: s\ndescription: Summarize\n{line}")
        assert _names(ws, home) == ["s"]

    @pytest.mark.parametrize(
        "line",
        [
            "disable-model-invocation: true",
            "user-invocable: false",
            "user-invocable: 'true'",
            "model: haiku",
            "tools: Read",
            "disallowed-tools: Bash",
            "hooks: {PreToolUse: []}",
            "context: fork",
            "agent: Explore",
            "globs: '*.py'",
        ],
    )
    def test_key_it_cannot_honor_skips(
        self, dirs: tuple[Path, Path], line: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        ws, home = dirs
        _claude_skill(home, "s", f"name: s\ndescription: Summarize\n{line}")
        with caplog.at_level(logging.WARNING, logger="tstd.skills"):
            assert _names(ws, home) == []
        key = line.split(":", 1)[0]
        assert any(r.__dict__.get("extra_fields", {}).get("keys") == [key] for r in caplog.records)


class TestCatalog:
    def test_description_is_not_repeated_as_when_to_use(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_skill(home, "s", "name: s\ndescription: Summarize a file")
        catalog = render_skill_catalog(SkillDiscoverer(home_dir=home).discover(ws))
        assert "- `s`: Summarize a file\n" in catalog + "\n"
        assert "when to use" not in catalog

    def test_distinct_when_to_use_is_kept(self, dirs: tuple[Path, Path]) -> None:
        ws, home = dirs
        _claude_skill(home, "s", "description: Summarize\nwhenToUse: After a long read")
        catalog = render_skill_catalog(SkillDiscoverer(home_dir=home).discover(ws))
        assert "- `s`: Summarize (when to use: After a long read)" in catalog
