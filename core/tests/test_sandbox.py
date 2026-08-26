"""Rootless container gate (TD-4301)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from tstd.autonomy.sandbox import (
    INSTALL_DOWN,
    INSTALL_MISSING,
    INSTALL_ROOTFUL,
    WORKSPACE_DEST,
    RuntimeState,
    SandboxError,
    container_argv,
    sandbox_exec,
    sandbox_start_error,
)
from tstd.config import AutonomyConfig

CORE = Path(__file__).resolve().parent.parent / "tstd"


def _cfg(
    *, runtime: str = "podman", image: str = "docker.io/library/alpine:3.21"
) -> AutonomyConfig:
    return AutonomyConfig(runtime=runtime, image=image)


def _which_none(_name: str) -> str | None:
    return None


def _fake_runtime(tmp_path: Path, *, info_out: str = "true", info_rc: int = 0) -> Path:
    """An executable that speaks enough of ``podman info`` / ``podman run``."""
    script = tmp_path / "podman"
    log = tmp_path / "argv.log"
    script.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import sys",
                "from pathlib import Path",
                f"Path({str(log)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')",
                "if len(sys.argv) > 1 and sys.argv[1] == 'info':",
                f"    print({info_out!r})",
                f"    raise SystemExit({info_rc})",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _which_for(path: Path):
    def _which(name: str) -> str | None:
        if name == "podman" or name == path.name:
            return str(path)
        return None

    return _which


# ── Probe / start refusal ────────────────────────────────────────────────


class TestSandboxStartError:
    async def test_missing_runtime_returns_install_copy(self) -> None:
        err = await sandbox_start_error(_cfg(), which=_which_none)
        assert err == INSTALL_MISSING
        assert "Install Podman" in err
        assert "brew install podman" in err
        assert "Interactive sessions do not need this" in err

    async def test_down_runtime_names_machine_start(self, tmp_path: Path) -> None:
        async def _down(_binary: str) -> RuntimeState:
            return RuntimeState.DOWN

        err = await sandbox_start_error(
            _cfg(), which=_which_for(_fake_runtime(tmp_path)), inspect=_down
        )
        assert err == INSTALL_DOWN
        assert "podman machine start" in err

    async def test_rootful_runtime_is_refused(self, tmp_path: Path) -> None:
        async def _rootful(_binary: str) -> RuntimeState:
            return RuntimeState.ROOTFUL

        err = await sandbox_start_error(
            _cfg(), which=_which_for(_fake_runtime(tmp_path)), inspect=_rootful
        )
        assert err == INSTALL_ROOTFUL

    async def test_rootless_runtime_is_live(self, tmp_path: Path) -> None:
        async def _ok(_binary: str) -> RuntimeState:
            return RuntimeState.ROOTLESS

        err = await sandbox_start_error(
            _cfg(), which=_which_for(_fake_runtime(tmp_path)), inspect=_ok
        )
        assert err is None

    async def test_inspects_the_fake_binary(self, tmp_path: Path) -> None:
        fake = _fake_runtime(tmp_path, info_out="true")
        err = await sandbox_start_error(_cfg(), which=_which_for(fake))
        assert err is None
        recorded = (tmp_path / "argv.log").read_text(encoding="utf-8")
        assert recorded.splitlines()[0] == "info"


# ── Argv contract ────────────────────────────────────────────────────────


class TestContainerArgv:
    def test_only_the_workspace_is_mounted(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = container_argv(
            ws,
            runtime="podman",
            image="docker.io/library/alpine:3.21",
            inner=("true",),
        )
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--mount"]
        assert mounts == [f"type=bind,src={ws.resolve()},dst={WORKSPACE_DEST}"]
        assert "--workdir" in argv
        assert argv[argv.index("--workdir") + 1] == WORKSPACE_DEST
        joined = " ".join(argv)
        assert "--network=none" in argv
        assert "--userns=keep-id" in argv
        assert "--pull" in argv and argv[argv.index("--pull") + 1] == "never"
        assert "--privileged" not in argv
        assert "--network=host" not in argv
        assert "-v" not in argv
        assert "--volume" not in argv
        assert "$HOME" not in joined
        assert str(Path.home()) not in joined
        assert argv[-1] == "true"
        assert argv[-2] == "docker.io/library/alpine:3.21"

    def test_empty_inner_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="empty"):
            container_argv(tmp_path, runtime="podman", image="alpine", inner=())

    def test_comma_in_workspace_is_refused(self, tmp_path: Path) -> None:
        ws = tmp_path / "a,b"
        ws.mkdir()
        with pytest.raises(SandboxError, match="comma"):
            container_argv(ws, runtime="podman", image="alpine", inner=("true",))


# ── Exec through a fake runtime ──────────────────────────────────────────


class TestSandboxExec:
    async def test_exec_uses_the_argv_contract(self, tmp_path: Path) -> None:
        fake = _fake_runtime(tmp_path)
        ws = tmp_path / "project"
        ws.mkdir()

        async def _ok(_binary: str) -> RuntimeState:
            return RuntimeState.ROOTLESS

        result = await sandbox_exec(
            ws,
            ("true",),
            config=_cfg(),
            which=_which_for(fake),
            inspect=_ok,
        )
        assert result.returncode == 0
        assert result.argv[0] == str(fake.resolve())
        assert "--network=none" in result.argv
        mounts = [result.argv[i + 1] for i, part in enumerate(result.argv) if part == "--mount"]
        assert mounts == [f"type=bind,src={ws.resolve()},dst={WORKSPACE_DEST}"]

    async def test_missing_runtime_does_not_exec(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="Install Podman"):
            await sandbox_exec(tmp_path, ("true",), config=_cfg(), which=_which_none)


# ── Interactive path stays clear ─────────────────────────────────────────


class TestInteractiveDoesNotNeedAContainer:
    def test_loop_and_session_do_not_import_sandbox(self) -> None:
        for name in ("loop.py", "session.py"):
            text = (CORE / name).read_text(encoding="utf-8")
            assert "sandbox" not in text, f"{name} must not mention the sandbox"

    def test_daemon_sandbox_is_only_the_start_button(self) -> None:
        text = (CORE / "daemon.py").read_text(encoding="utf-8")
        assert "from .autonomy.sandbox" not in text
        start = text.index("async def _handle_start_autonomy")
        rest = text[start:]
        next_def = rest.find("\n    async def ", 1)
        body = rest if next_def == -1 else rest[:next_def]
        before = text[:start]
        assert "sandbox" not in before
        assert "run_autonomy_start" in body


# ── Config surface ───────────────────────────────────────────────────────


class TestAutonomyConfig:
    def test_defaults_match_shipped_names(self) -> None:
        cfg = AutonomyConfig()
        assert cfg.runtime == "podman"
        assert cfg.image == "docker.io/library/alpine:3.21"
        assert cfg.check_every == 5
        assert cfg.verify == "after_write"

    def test_blank_runtime_is_rejected(self) -> None:
        with pytest.raises(Exception, match="empty"):
            AutonomyConfig(runtime="  ", image="alpine")
