"""Rootless container gate (TD-4301, TD-4302)."""

from __future__ import annotations

import inspect
import stat
import sys
from pathlib import Path

import pytest

from tests.test_loop import make_config
from tstd.autonomy.sandbox import (
    INSTALL_DOWN,
    INSTALL_MISSING,
    INSTALL_ROOTFUL,
    WORKSPACE_DEST,
    RuntimeState,
    SandboxError,
    autonomy_shell_argv,
    container_argv,
    sandbox_exec,
    sandbox_start_error,
)
from tstd.config import AutonomyConfig
from tstd.logging import user_data_dir
from tstd.session import Session
from tstd.tools.shell import run_shell

CORE = Path(__file__).resolve().parent.parent / "tstd"

# Host paths that must never appear in the container argv (TD-4302).
_CRED_FRAGMENTS = (
    ".ssh",
    ".aws",
    ".config/gcloud",
    "credentials.json",
    "login.keyring",
    "Library/Keychains",
    "remote-token",
)


def _cfg(
    *, runtime: str = "podman", image: str = "docker.io/library/alpine:3.21"
) -> AutonomyConfig:
    return AutonomyConfig(runtime=runtime, image=image)


def _which_none(_name: str) -> str | None:
    return None


def _fake_runtime(tmp_path: Path, *, info_out: str = "true", info_rc: int = 0) -> Path:
    """An executable that speaks enough of ``podman info`` / ``podman run``.

    On ``run``, prints whether a well-known host cred path leaked into
    argv — the container-view stand-in when Podman is not on PATH.
    """
    log = tmp_path / "argv.log"
    helper = tmp_path / "_podman_fake.py"
    helper.write_text(
        "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                f"Path({str(log)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')",
                "if len(sys.argv) > 1 and sys.argv[1] == 'info':",
                f"    print({info_out!r})",
                f"    raise SystemExit({info_rc})",
                "joined = ' '.join(sys.argv[1:])",
                "home = str(Path.home())",
                "markers = (",
                "    home + '/.ssh',",
                "    str(Path.home() / '.ssh'),",
                "    '/root/.ssh',",
                "    '$HOME/.ssh',",
                "    '~/.ssh',",
                ")",
                "visible = any(m in joined for m in markers)",
                "print('cred_path_visible' if visible else 'cred_path_absent')",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        script = tmp_path / "podman.cmd"
        script.write_text(f'@"{sys.executable}" "{helper}" %*\r\n', encoding="utf-8")
    else:
        script = tmp_path / "podman"
        script.write_text(
            f"#!{sys.executable}\n" + helper.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _assert_no_host_creds(argv: list[str]) -> None:
    """The argv must not mention host creds or extra mounts."""
    joined = " ".join(argv)
    if sys.platform != "win32":
        assert str(Path.home()) not in joined
    assert "$HOME" not in joined
    assert "~/.ssh" not in joined
    assert "/root/.ssh" not in joined
    assert "$HOME/.ssh" not in joined
    for fragment in _CRED_FRAGMENTS:
        assert fragment not in joined
    home_ssh = str(Path.home() / ".ssh")
    assert home_ssh not in joined
    data = str(user_data_dir())
    assert data not in joined
    assert "-v" not in argv
    assert "--volume" not in argv
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--mount"]
    assert len(mounts) == 1
    assert mounts[0].startswith("type=bind,src=")
    assert mounts[0].endswith(f",dst={WORKSPACE_DEST}")


def _which_for(path: Path):
    def _which(name: str) -> str | None:
        if name in {path.name, path.stem, "podman"}:
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
        assert "--network=none" in argv
        assert "--userns=keep-id" in argv
        assert "--pull" in argv and argv[argv.index("--pull") + 1] == "never"
        assert "--privileged" not in argv
        assert "--network=host" not in argv
        _assert_no_host_creds(argv)
        assert argv[-1] == "true"
        assert argv[-2] == "docker.io/library/alpine:3.21"

    def test_no_api_can_mount_home_or_extra_paths(self) -> None:
        params = inspect.signature(container_argv).parameters
        for banned in ("mounts", "extra_mounts", "volumes", "home", "binds"):
            assert banned not in params

    def test_empty_inner_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="empty"):
            container_argv(tmp_path, runtime="podman", image="alpine", inner=())

    def test_comma_in_workspace_is_refused(self, tmp_path: Path) -> None:
        ws = tmp_path / "a,b"
        ws.mkdir()
        with pytest.raises(SandboxError, match="comma"):
            container_argv(ws, runtime="podman", image="alpine", inner=("true",))

    def test_deny_network_is_none(self, tmp_path: Path) -> None:
        argv = container_argv(
            tmp_path, runtime="podman", image="alpine", inner=("true",), network="deny"
        )
        assert "--network=none" in argv
        assert "--network=host" not in argv

    def test_allowlist_uses_slirp_not_host(self, tmp_path: Path) -> None:
        argv = container_argv(
            tmp_path,
            runtime="podman",
            image="alpine",
            inner=("true",),
            network=["api.example.com", "pypi.org"],
        )
        assert "--network=none" not in argv
        assert "--network=host" not in argv
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--mount"]
        assert mounts == [f"type=bind,src={tmp_path.resolve()},dst={WORKSPACE_DEST}"]
        _assert_no_host_creds(argv)

    def test_unexpected_network_fails_closed_as_deny(self, tmp_path: Path) -> None:
        for network in ([], "allow", "host", ["", "  "]):
            argv = container_argv(
                tmp_path, runtime="podman", image="alpine", inner=("true",), network=network
            )
            assert "--network=none" in argv, network
            assert "--network=host" not in argv


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
        _assert_no_host_creds(result.argv)
        assert "cred_path_absent" in result.stdout
        assert "cred_path_visible" not in result.stdout

    async def test_well_known_cred_path_is_absent_from_the_container(self, tmp_path: Path) -> None:
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
        joined = " ".join(result.argv)
        home_ssh = str(Path.home() / ".ssh")
        assert home_ssh not in joined
        assert "~/.ssh" not in joined
        assert "/root/.ssh" not in joined
        assert "$HOME/.ssh" not in joined
        assert "cred_path_absent" in result.stdout
        assert "cred_path_visible" not in result.stdout
        _assert_no_host_creds(result.argv)

    async def test_allowlist_exec_omits_network_none(self, tmp_path: Path) -> None:
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
            network=["api.example.com"],
        )
        assert "--network=none" not in result.argv
        assert "--network=host" not in result.argv
        _assert_no_host_creds(result.argv)
        assert "cred_path_absent" in result.stdout

    async def test_missing_runtime_does_not_exec(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="Install Podman"):
            await sandbox_exec(tmp_path, ("true",), config=_cfg(), which=_which_none)


# ── Autonomy shell uses the container (TD-4301 act seam) ─────────────────


class TestAutonomyShell:
    def test_argv_is_container_not_host_shell(self, tmp_path: Path) -> None:
        fake = _fake_runtime(tmp_path)
        ws = tmp_path / "ws"
        ws.mkdir()
        argv = autonomy_shell_argv(
            ws,
            "true",
            runtime="podman",
            image="alpine",
            which=_which_for(fake),
        )
        assert argv[0] == str(fake)
        assert argv[-3:] == ["/bin/sh", "-c", "true"]
        _assert_no_host_creds(argv)
        assert f"dst={WORKSPACE_DEST}" in " ".join(argv)

    def test_missing_runtime_refuses_before_exec(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxError, match="Install Podman"):
            autonomy_shell_argv(
                tmp_path,
                "true",
                runtime="podman",
                image="alpine",
                which=_which_none,
            )

    async def test_run_shell_autonomy_does_not_see_host_creds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _fake_runtime(tmp_path)
        monkeypatch.setattr("tstd.autonomy.sandbox.shutil.which", _which_for(fake))
        ws = tmp_path / "ws"
        ws.mkdir()
        session = Session(str(ws))
        session.autonomy = True
        session.config = make_config()
        result = await run_shell(session, "true")
        assert "cred_path_absent" in result
        recorded = (tmp_path / "argv.log").read_text(encoding="utf-8")
        assert "run" in recorded
        assert "--mount" in recorded or "type=bind" in recorded
        assert ".ssh" not in recorded

    async def test_run_shell_interactive_does_not_need_podman(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tstd.autonomy.sandbox.shutil.which", _which_none)
        session = Session(str(tmp_path))
        result = await run_shell(session, "echo host-ok")
        assert "host-ok" in result


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
