"""Sign-and-start gate (TD-4003)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_charter import VALID_FRONTMATTER, _write_charter
from tests.test_charter_editor import _VALID, _save
from tests.test_checkpoint import _git, make_repo
from tests.test_sandbox import _fake_runtime, _which_for, _which_none
from tstd.autonomy.charter import CHARTER_COMMIT_SUBJECT, CHARTER_RELATIVE_PARTS, charter_path
from tstd.autonomy.sandbox import INSTALL_MISSING, RuntimeState
from tstd.autonomy.start import run_autonomy_start
from tstd.config import AutonomyConfig
from tstd.daemon import Daemon
from tstd.tools.registry import create_registry

CORE = Path(__file__).resolve().parent.parent / "tstd"


def _start(
    workspace: Path,
    charter: dict[str, object] | None = None,
    notes: str = "",
) -> str:
    payload: dict[str, object] = {
        "type": "start_autonomy",
        "workspace_path": str(workspace),
        "notes": notes,
    }
    if charter is not None:
        payload["charter"] = charter
    return json.dumps(payload)


async def _ok(_binary: str) -> RuntimeState:
    return RuntimeState.ROOTLESS


# ── Library gate ─────────────────────────────────────────────────────────


class TestRunAutonomyStart:
    async def test_missing_charter_is_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        result = await run_autonomy_start(
            repo, config=AutonomyConfig(), which=_which_none, inspect=_ok
        )
        assert result.ready is False
        assert result.signed is False
        assert result.error is not None
        assert "CHARTER.md" in result.error

    async def test_unsigned_charter_is_signed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        _write_charter(repo, VALID_FRONTMATTER)
        fake = _fake_runtime(tmp_path)
        before = _git(repo, "rev-parse", "HEAD")
        result = await run_autonomy_start(
            repo,
            config=AutonomyConfig(),
            which=_which_for(fake),
            inspect=_ok,
        )
        assert result.ready is True
        assert result.signed is True
        assert result.error is None
        after = _git(repo, "rev-parse", "HEAD")
        assert after != before
        assert CHARTER_COMMIT_SUBJECT in _git(repo, "log", "-1", "--format=%s")
        rel = "/".join(CHARTER_RELATIVE_PARTS)
        assert rel in _git(repo, "ls-files", "--", rel)

    async def test_missing_runtime_refuses_after_sign(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        _write_charter(repo, VALID_FRONTMATTER)
        result = await run_autonomy_start(
            repo, config=AutonomyConfig(), which=_which_none, inspect=_ok
        )
        assert result.ready is False
        assert result.signed is True
        assert result.error == INSTALL_MISSING
        assert CHARTER_COMMIT_SUBJECT in _git(repo, "log", "-1", "--format=%s")

    async def test_write_then_sign_from_mapping(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        fake = _fake_runtime(tmp_path)
        result = await run_autonomy_start(
            repo,
            config=AutonomyConfig(),
            charter=_VALID,
            notes="From the start button.",
            which=_which_for(fake),
            inspect=_ok,
        )
        assert result.ready is True
        assert charter_path(repo).is_file()
        assert "From the start button." in charter_path(repo).read_text(encoding="utf-8")


# ── Daemon verb ──────────────────────────────────────────────────────────


class TestDaemonStartAutonomy:
    async def test_refuses_invalid_charter_without_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import default_config_yaml, load_config

        repo = make_repo(tmp_path)
        before = _git(repo, "rev-parse", "HEAD")
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(default_config_yaml(), encoding="utf-8")
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: load_config(cfg_path))

        async def _no_launch(_self: object, _workspace: Path, _charter: object) -> str:
            raise AssertionError("invalid charter must not launch a run")

        monkeypatch.setattr("tstd.daemon.Daemon._launch_autonomy_run", _no_launch)
        daemon = Daemon(data_dir=tmp_path / "data")
        bad = dict(_VALID)
        bad["version"] = 1
        raw = await daemon._handle_message(_start(repo, bad), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["code"] == "invalid_charter"
        assert "version" in payload["message"]
        assert _git(repo, "rev-parse", "HEAD") == before
        await daemon._shutdown()

    async def test_start_event_names_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import default_config_yaml, load_config

        repo = make_repo(tmp_path)
        fake = _fake_runtime(tmp_path)
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(default_config_yaml(), encoding="utf-8")
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: load_config(cfg_path))
        monkeypatch.setattr("tstd.autonomy.sandbox.shutil.which", _which_for(fake))

        async def _rootless(_binary: str) -> RuntimeState:
            return RuntimeState.ROOTLESS

        monkeypatch.setattr("tstd.autonomy.sandbox.inspect_runtime", _rootless)

        async def _fake_launch(_self: object, _workspace: Path, _charter: object) -> str:
            return "sess-autonomy"

        monkeypatch.setattr("tstd.daemon.Daemon._launch_autonomy_run", _fake_launch)
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(_start(repo, _VALID), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["type"] == "autonomy_start"
        assert payload["ready"] is True
        assert payload["signed"] is True
        assert payload["error"] is None
        assert payload["session_id"] == "sess-autonomy"
        await daemon._shutdown()

    async def test_sandbox_refusal_does_not_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import default_config_yaml, load_config

        repo = make_repo(tmp_path)
        _write_charter(repo, VALID_FRONTMATTER)
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(default_config_yaml(), encoding="utf-8")
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: load_config(cfg_path))
        monkeypatch.setattr("tstd.autonomy.sandbox.shutil.which", _which_none)

        async def _no_launch(_self: object, _workspace: Path, _charter: object) -> str:
            raise AssertionError("a refused start must not launch a run")

        monkeypatch.setattr("tstd.daemon.Daemon._launch_autonomy_run", _no_launch)
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(_start(repo), None)
        assert raw is not None
        payload = json.loads(raw)
        assert payload["type"] == "autonomy_start"
        assert payload["ready"] is False
        assert payload["session_id"] is None
        await daemon._shutdown()

    async def test_save_still_does_not_commit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = _git(repo, "rev-parse", "HEAD")
        daemon = Daemon(data_dir=tmp_path / "data")
        await daemon._handle_message(_save(repo), None)
        assert _git(repo, "rev-parse", "HEAD") == before
        await daemon._shutdown()

    def test_not_in_tool_registry(self) -> None:
        names = {tool.name for tool in create_registry().list_tools()}
        assert "start_autonomy" not in names


class TestInteractiveUnchanged:
    def test_loop_and_session_do_not_own_the_start_verb(self) -> None:
        for name in ("loop.py", "session.py"):
            text = (CORE / name).read_text(encoding="utf-8")
            assert "start_autonomy" not in text
            assert "run_autonomy_start" not in text
            assert "sandbox" not in text
