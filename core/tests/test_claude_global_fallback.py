"""User-global Claude Code fallback is opt-in (TD-4845).

The default resolver and the shipped config do not open
``~/.claude/CLAUDE.md`` or anything it imports. Enabling
``steering.claude_global_fallback`` restores the TD-502 fallback,
including the class C approval for an import outside the workspace.
``~/.tstdesk/AGENTS.md`` still wins. Workspace ``CLAUDE.md`` is not gated.
Doctor names the unread file only in the not-loaded case.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tstd.config import SteeringConfig, default_config_yaml, load_config
from tstd.context import ContextAssembler, Precedence, SteeringFileResolver
from tstd.context.discover import CLAUDE_GLOBAL_NOT_LOADED
from tstd.context.prompt import PromptAssembler
from tstd.daemon import Daemon


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _assemble(
    home: Path,
    workspace: Path,
    *,
    claude_global_fallback: bool = False,
) -> ContextAssembler:
    return ContextAssembler(
        resolver=SteeringFileResolver(
            home_dir=home,
            claude_global_fallback=claude_global_fallback,
        )
    )


def _home_with_claude(base: Path, body: str) -> tuple[Path, Path, Path]:
    """Home with only ``~/.claude/CLAUDE.md``, plus an empty workspace."""
    home = base / "home"
    workspace = base / "workspace"
    workspace.mkdir()
    claude = home / ".claude" / "CLAUDE.md"
    _write(claude, body)
    return home, workspace, claude


class TestDefaultDoesNotLoadClaudeGlobal:
    """The home-dir seam, with the flag omitted, is the shipped default."""

    def test_omitted_flag_does_not_open_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home, workspace, claude = _home_with_claude(tmp_path, "GLOBAL LEAK\n@rules-core.md\n")
        imported = home / ".claude" / "rules-core.md"
        _write(imported, "IMPORT LEAK\n")
        forbidden = {claude.resolve(), imported.resolve()}
        real_read = Path.read_text

        def guarded(
            self: Path,
            encoding: str | None = None,
            errors: str | None = None,
        ) -> str:
            if self.resolve() in forbidden:
                raise AssertionError(f"discovery read {self}")
            return real_read(self, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", guarded)
        # No claude_global_fallback argument: the constructor default is off.
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(
            workspace
        )
        assert result.sources == []
        assert result.block == ""
        assert result.pending_imports == ()
        assert result.notices == (CLAUDE_GLOBAL_NOT_LOADED,)
        assert "not loaded — enable steering.claude_global_fallback to use it" in result.notices[0]

    def test_tstdesk_agents_wins_and_does_not_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home, workspace, claude = _home_with_claude(tmp_path, "GLOBAL LEAK\n")
        _write(home / ".tstdesk" / "AGENTS.md", "Prefer TST.\n")
        forbidden = {claude.resolve()}
        real_read = Path.read_text

        def guarded(
            self: Path,
            encoding: str | None = None,
            errors: str | None = None,
        ) -> str:
            if self.resolve() in forbidden:
                raise AssertionError(f"discovery read {self}")
            return real_read(self, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", guarded)
        result = ContextAssembler(resolver=SteeringFileResolver(home_dir=home)).assemble_sync(
            workspace
        )
        assert [s.path.name for s in result.sources] == ["AGENTS.md"]
        assert result.sources[0].shadowed_path is None
        assert result.sources[0].precedence == Precedence.USER_GLOBAL
        assert "Prefer TST." in result.block
        assert "GLOBAL LEAK" not in result.block
        assert result.notices == ()

    @pytest.mark.parametrize(
        ("agents", "claude", "expect_notice"),
        [
            (False, False, False),
            (False, True, True),
            (True, True, False),
            (True, False, False),
        ],
    )
    def test_notice_only_when_claude_is_the_unread_global(
        self, tmp_path: Path, agents: bool, claude: bool, expect_notice: bool
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        if agents:
            _write(home / ".tstdesk" / "AGENTS.md", "ours\n")
        if claude:
            _write(home / ".claude" / "CLAUDE.md", "theirs\n")
        result = _assemble(home, workspace).assemble_sync(workspace)
        assert (result.notices == (CLAUDE_GLOBAL_NOT_LOADED,)) is expect_notice
        assert "theirs" not in result.block


class TestEnabledRestoresFallback:
    def test_loads_global_claude_and_gates_an_outside_import(self, tmp_path: Path) -> None:
        home, workspace, _claude = _home_with_claude(tmp_path, "Ask first.\n@rules-core.md\n")
        imported = home / ".claude" / "rules-core.md"
        _write(imported, "IMPORT LEAK\n")
        result = _assemble(home, workspace, claude_global_fallback=True).assemble_sync(workspace)
        assert len(result.sources) == 1
        assert result.sources[0].is_fallback is True
        assert result.sources[0].path == home / ".claude" / "CLAUDE.md"
        assert "Ask first." in result.block
        assert "IMPORT LEAK" not in result.block
        assert imported.resolve() in result.pending_imports
        assert any("awaiting approval" in issue for issue in result.import_issues)
        assert result.notices == ()

    def test_agents_still_wins_and_records_the_shadow(self, tmp_path: Path) -> None:
        home, workspace, claude = _home_with_claude(tmp_path, "GLOBAL LEAK\n")
        _write(home / ".tstdesk" / "AGENTS.md", "Prefer TST.\n")
        result = _assemble(home, workspace, claude_global_fallback=True).assemble_sync(workspace)
        assert result.sources[0].path.name == "AGENTS.md"
        assert result.sources[0].shadowed_path == claude
        assert "GLOBAL LEAK" not in result.block
        assert result.notices == ()

    def test_prompt_assembler_forwards_the_flag(self, tmp_path: Path) -> None:
        """The loop and the instruction-stack handler pass the flag here."""
        home, workspace, _claude = _home_with_claude(tmp_path, "GLOBAL LEAK\n")
        off = PromptAssembler(workspace, home_dir=home).assemble_sync("brain")
        on = PromptAssembler(workspace, home_dir=home, claude_global_fallback=True).assemble_sync(
            "brain"
        )
        assert "GLOBAL LEAK" not in off.text
        assert off.steering.notices == (CLAUDE_GLOBAL_NOT_LOADED,)
        assert "GLOBAL LEAK" in on.text
        assert on.steering.notices == ()


class TestWorkspaceClaudeUnaffected:
    def test_workspace_and_nested_claude_still_load(self, tmp_path: Path) -> None:
        home, workspace, _claude = _home_with_claude(tmp_path, "GLOBAL LEAK\n")
        _write(workspace / "CLAUDE.md", "Rebase.\n")
        _write(workspace / "src" / "CLAUDE.md", "Nested.\n")
        result = _assemble(home, workspace).assemble_sync(workspace)
        names = [(s.precedence, s.path.name, s.is_fallback) for s in result.sources]
        assert names == [
            (Precedence.WORKSPACE, "CLAUDE.md", True),
            (Precedence.NESTED, "CLAUDE.md", True),
        ]
        assert "Rebase." in result.block
        assert "Nested." in result.block
        assert "GLOBAL LEAK" not in result.block
        assert result.notices == (CLAUDE_GLOBAL_NOT_LOADED,)


class TestShippedConfig:
    def test_shipped_default_is_off(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(default_config_yaml(), encoding="utf-8")
        assert load_config(path).steering.claude_global_fallback is False

    def test_omitted_section_stays_off(self, tmp_path: Path) -> None:
        data = yaml.safe_load(default_config_yaml())
        assert isinstance(data, dict)
        del data["steering"]
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert load_config(path).steering.claude_global_fallback is False

    def test_true_round_trips(self, tmp_path: Path) -> None:
        data = yaml.safe_load(default_config_yaml())
        assert isinstance(data, dict)
        data["steering"]["claude_global_fallback"] = True
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert load_config(path).steering.claude_global_fallback is True


def _daemon(tmp_path: Path, *, enabled: bool) -> Daemon:
    """A daemon whose steering flag is set without touching the cached config."""
    daemon = Daemon(data_dir=tmp_path / "data")
    daemon.config = daemon.config.model_copy(
        update={"steering": SteeringConfig(claude_global_fallback=enabled)}
    )
    return daemon


class TestDoctorNotice:
    @pytest.mark.asyncio
    async def test_ok_row_names_the_unread_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home, _workspace, _claude = _home_with_claude(tmp_path, "GLOBAL LEAK\n@rules-core.md\n")
        _write(home / ".claude" / "rules-core.md", "IMPORT LEAK\n")
        # Build the daemon before the home patch so config load stays on
        # the real user-data dir. The check itself resolves ~/.claude.
        daemon = _daemon(tmp_path, enabled=False)
        monkeypatch.setattr(Path, "home", lambda: home)
        check = await daemon._check_steering(tmp_path / "workspace")
        assert check.status == "ok"
        assert check.fix is None
        assert CLAUDE_GLOBAL_NOT_LOADED in check.detail
        assert "GLOBAL LEAK" not in check.detail
        assert "IMPORT LEAK" not in check.detail

    @pytest.mark.asyncio
    async def test_enabled_row_does_not_say_not_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home, _workspace, _claude = _home_with_claude(tmp_path, "Ask first.\n@rules-core.md\n")
        _write(home / ".claude" / "rules-core.md", "IMPORT LEAK\n")
        daemon = _daemon(tmp_path, enabled=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        check = await daemon._check_steering(tmp_path / "workspace")
        assert "not loaded" not in check.detail
        assert check.status == "fail"
        assert "awaiting approval" in check.detail
        assert "rules-core.md" in check.detail
        assert "IMPORT LEAK" not in check.detail

    @pytest.mark.asyncio
    async def test_broken_import_keeps_the_notice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        _write(home / ".claude" / "CLAUDE.md", "GLOBAL LEAK\n")
        _write(workspace / "AGENTS.md", "@missing.md\n")
        daemon = _daemon(tmp_path, enabled=False)
        monkeypatch.setattr(Path, "home", lambda: home)
        check = await daemon._check_steering(workspace)
        assert check.status == "fail"
        assert "missing.md" in check.detail
        assert CLAUDE_GLOBAL_NOT_LOADED in check.detail

    @pytest.mark.asyncio
    async def test_no_notice_when_our_global_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _write(home / ".tstdesk" / "AGENTS.md", "ours\n")
        _write(home / ".claude" / "CLAUDE.md", "GLOBAL LEAK\n")
        daemon = _daemon(tmp_path, enabled=False)
        monkeypatch.setattr(Path, "home", lambda: home)
        check = await daemon._check_steering(workspace)
        assert check.status == "ok"
        assert "not loaded" not in check.detail
        assert "GLOBAL LEAK" not in check.detail
