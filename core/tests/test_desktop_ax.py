"""The agent-desktop CLI boundary and tree shaping (TD-5220).

What runs (checksum), with what environment, how a refusal maps to a
``DesktopError``, when a retry is allowed, and how a noisy tree becomes
a compact one.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from tests.ax_fakes import FakeCli, fixture, make_cli
from tstd.desktop import DesktopError, ax_tree
from tstd.desktop.ax_cli import (
    PINNED_SHA256,
    AgentDesktopCli,
    child_env,
    host_target,
    resolve_binary,
)


class TestChecksum:
    async def test_mismatch_never_runs(self, tmp_path: Path) -> None:
        fake = FakeCli({"list-windows": "list_windows.json"})
        binary = tmp_path / "agent-desktop"
        binary.write_bytes(b"something else")
        cli = AgentDesktopCli(binary, sha256="0" * 64, runner=fake)
        with pytest.raises(DesktopError) as err:
            await cli.run("list-windows")
        assert err.value.code == "ax_checksum"
        assert fake.calls == []

    async def test_unconfigured_digest_must_be_a_pinned_release(self, tmp_path: Path) -> None:
        fake = FakeCli({"list-windows": "list_windows.json"})
        binary = tmp_path / "agent-desktop"
        binary.write_bytes(b"not the release")
        with pytest.raises(DesktopError) as err:
            await AgentDesktopCli(binary, runner=fake).run("list-windows")
        assert err.value.code == "ax_checksum"
        assert fake.calls == []

    async def test_missing_binary_is_unavailable(self) -> None:
        with pytest.raises(DesktopError) as err:
            await AgentDesktopCli(None).run("list-windows")
        assert err.value.code == "ax_unavailable"

    async def test_unreadable_binary_is_unavailable(self, tmp_path: Path) -> None:
        cli = AgentDesktopCli(tmp_path / "absent", sha256="0" * 64)
        with pytest.raises(DesktopError) as err:
            await cli.run("list-windows")
        assert err.value.code == "ax_unavailable"

    async def test_matching_digest_runs(self, tmp_path: Path) -> None:
        fake = FakeCli({"list-windows": "list_windows.json"})
        data = await make_cli(tmp_path, fake).run("list-windows")
        assert isinstance(data, list) and len(data) == 4
        assert fake.commands() == ["list-windows"]

    async def test_a_replaced_binary_is_checked_again(self, tmp_path: Path) -> None:
        fake = FakeCli({"list-windows": "list_windows.json"})
        cli = make_cli(tmp_path, fake)
        await cli.run("list-windows")
        (tmp_path / "agent-desktop").write_bytes(b"swapped after the first check")
        with pytest.raises(DesktopError) as err:
            await cli.run("list-windows")
        assert err.value.code == "ax_checksum"
        assert fake.commands() == ["list-windows"]

    def test_this_host_has_a_pin(self) -> None:
        assert host_target() in PINNED_SHA256 or sys.platform not in ("linux", "darwin", "win32")
        assert all(len(v) == 64 for v in PINNED_SHA256.values())


class TestEnvironment:
    def test_drops_no_at_bridge_and_relocates_state(self, tmp_path: Path) -> None:
        env = child_env({"NO_AT_BRIDGE": "1", "DISPLAY": ":1"}, tmp_path / "ad")
        assert "NO_AT_BRIDGE" not in env
        assert env["DISPLAY"] == ":1"
        assert env["AGENT_DESKTOP_HOME"] == str(tmp_path / "ad")

    async def test_every_command_gets_the_clean_env(self, tmp_path: Path) -> None:
        fake = FakeCli({"list-windows": "list_windows.json"})
        await make_cli(tmp_path, fake).run("list-windows")
        assert "NO_AT_BRIDGE" not in fake.envs[0]
        assert fake.envs[0]["AGENT_DESKTOP_HOME"] == str(tmp_path / "state")
        assert (tmp_path / "state").is_dir()


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("name", "code"),
        [
            ("error_stale_ref.json", "ax_stale_ref"),
            ("error_snapshot_not_found.json", "ax_stale_ref"),
            ("error_ambiguous_target.json", "ax_ambiguous"),
            ("error_invalid_args.json", "ax_invalid_args"),
            ("error_window_not_found.json", "ax_not_found"),
            ("error_policy_denied.json", "ax_not_supported"),
            ("error_action_unverified.json", "ax_error"),
        ],
    )
    async def test_code(self, tmp_path: Path, name: str, code: str) -> None:
        fake = FakeCli({"click": name})
        with pytest.raises(DesktopError) as err:
            await make_cli(tmp_path, fake).run("click", "@s1:e1")
        assert err.value.code == code
        assert fake.commands() == ["click"], "a look-again or unsafe refusal is never retried"

    async def test_look_again_says_snapshot_again(self, tmp_path: Path) -> None:
        fake = FakeCli({"click": "error_stale_ref.json"})
        with pytest.raises(DesktopError) as err:
            await make_cli(tmp_path, fake).run("click", "@s1:e1")
        assert "desktop_snapshot" in err.value.message

    async def test_ambiguous_lists_candidates(self, tmp_path: Path) -> None:
        fake = FakeCli({"snapshot": "error_ambiguous_target.json"})
        with pytest.raises(DesktopError) as err:
            await make_cli(tmp_path, fake).run("snapshot", "--app", "xfce4-panel")
        assert "w-1.4-org-a11y-atspi-accessible-36" in err.value.message

    async def test_unsafe_delivery_warns_it_may_have_landed(self, tmp_path: Path) -> None:
        fake = FakeCli({"click": "error_action_unverified.json"})
        with pytest.raises(DesktopError) as err:
            await make_cli(tmp_path, fake).run("click", "@s1:e1")
        assert "may have landed" in err.value.message

    async def test_no_envelope_is_an_error_without_stderr(self, tmp_path: Path) -> None:
        async def runner(argv: list[str], env: object, limit: float) -> tuple[int, bytes, bytes]:
            return 2, b"panic", b"window title: secret"

        cli = make_cli(tmp_path, FakeCli({}))
        cli._runner = runner  # type: ignore[assignment]
        with pytest.raises(DesktopError) as err:
            await cli.run("snapshot")
        assert err.value.code == "ax_error"
        assert "secret" not in err.value.message


class TestRetry:
    async def test_safe_observation_timeout_is_retried_once(self, tmp_path: Path) -> None:
        fake = FakeCli({"snapshot": ["error_timeout.json", "snapshot_form.json"]})
        data = await make_cli(tmp_path, fake).run("snapshot", "--window-id", "w")
        assert isinstance(data, dict) and data["ref_count"] == 4
        assert fake.commands() == ["snapshot", "snapshot"]

    async def test_second_failure_is_raised(self, tmp_path: Path) -> None:
        fake = FakeCli({"snapshot": ["error_timeout.json", "error_timeout.json"]})
        with pytest.raises(DesktopError) as err:
            await make_cli(tmp_path, fake).run("snapshot")
        assert err.value.code == "ax_timeout"
        assert fake.commands() == ["snapshot", "snapshot"]

    async def test_action_needing_fresh_snapshot_is_not_retried(self, tmp_path: Path) -> None:
        fake = FakeCli({"click": ["error_timeout.json", "toggle_ok.json"]})
        with pytest.raises(DesktopError):
            await make_cli(tmp_path, fake).run("click", "@s1:e1")
        assert fake.commands() == ["click"]


@pytest.mark.skipif(sys.platform == "win32", reason="runs a shebang script")
async def test_real_subprocess_round_trip(tmp_path: Path) -> None:
    """The default runner: a real child process, real env, real checksum."""
    script = tmp_path / "agent-desktop"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'ok': True, 'data': {'argv': sys.argv[1:], "
        "'bridge': os.environ.get('NO_AT_BRIDGE'), "
        "'home': os.environ.get('AGENT_DESKTOP_HOME')}}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    cli = AgentDesktopCli(
        script,
        sha256=digest,
        state_dir=tmp_path / "state",
        base_env={**os.environ, "NO_AT_BRIDGE": "1"},
    )
    data = await cli.run("type", "--", "@s1:e1", "-5")
    assert data == {
        "argv": ["type", "--", "@s1:e1", "-5"],
        "bridge": None,
        "home": str(tmp_path / "state"),
    }


class TestResolveBinary:
    """Which CLI runs: the configured path, the bundled copy, then PATH (TD-5221)."""

    NAME = "agent-desktop.exe" if sys.platform == "win32" else "agent-desktop"

    def _layout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, frozen: bool) -> Path:
        """A packaged-daemon dir with the CLI beside tstd, and one more on PATH."""
        app = tmp_path / "app"
        app.mkdir()
        (app / "tstd").write_bytes(b"")
        on_path = tmp_path / "path"
        on_path.mkdir()
        for folder in (app, on_path):
            cli = folder / self.NAME
            cli.write_bytes(b"cli")
            cli.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(app / "tstd"))
        monkeypatch.setattr(sys, "frozen", frozen, raising=False)
        monkeypatch.setenv("PATH", str(on_path))
        return app

    def test_configured_path_wins_over_bundled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._layout(tmp_path, monkeypatch, frozen=True)
        assert resolve_binary("/opt/elsewhere/agent-desktop") == Path(
            "/opt/elsewhere/agent-desktop"
        )

    def test_bundled_copy_wins_over_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = self._layout(tmp_path, monkeypatch, frozen=True)
        assert resolve_binary("") == (app / self.NAME).resolve()

    def test_path_when_nothing_is_bundled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = self._layout(tmp_path, monkeypatch, frozen=True)
        (app / self.NAME).unlink()
        found = resolve_binary("")
        assert found is not None and found.parent == tmp_path / "path"

    def test_source_run_ignores_the_interpreter_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not frozen: sys.executable is Python, and its folder is not the bundle."""
        self._layout(tmp_path, monkeypatch, frozen=False)
        found = resolve_binary("")
        assert found is not None and found.parent == tmp_path / "path"

    def test_none_when_missing_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = self._layout(tmp_path, monkeypatch, frozen=True)
        (app / self.NAME).unlink()
        (tmp_path / "path" / self.NAME).unlink()
        assert resolve_binary("") is None


class TestTree:
    def test_form_keeps_every_control(self) -> None:
        tree = fixture("snapshot_form.json")["data"]["tree"]  # type: ignore[index]
        lines = ax_tree.render(ax_tree.prune(tree))  # type: ignore[arg-type]
        text = "\n".join(lines)
        for ref in ("e1", "e2", "e3", "e4"):
            assert f"@s3wspdr2z8h6x:{ref}" in text
        assert 'textfield "Password" @s3wspdr2z8h6x:e2 [secure]' in text

    def test_secure_value_never_rendered(self) -> None:
        node = {"role": "textfield", "name": "Password", "states": ["secure"], "value": "hunter2"}
        pruned = ax_tree.prune({"role": "window", "children": [node]})
        assert pruned is not None
        assert "hunter2" not in "\n".join(ax_tree.render(pruned))
        role_only = {"role": "password_text", "name": "PIN", "value": "1234", "ref_id": "@s:e1"}
        assert ax_tree.prune(role_only) is not None
        assert "1234" not in "\n".join(ax_tree.render(ax_tree.prune(role_only) or {}))

    def test_editor_menus_fold(self) -> None:
        data = fixture("snapshot_editor.json")["data"]
        tree = data["tree"]  # type: ignore[index]
        raw = json.dumps(tree)
        text = "\n".join(ax_tree.render(ax_tree.prune(tree)))  # type: ignore[arg-type]
        assert "items folded" in text
        assert len(text) < len(raw) / 4, (len(text), len(raw))
        assert "textfield" in text

    def test_web_noise_dropped(self) -> None:
        tree = fixture("snapshot_web_login.json")["data"]["tree"]  # type: ignore[index]
        pruned = ax_tree.prune(tree)  # type: ignore[arg-type]
        assert pruned is not None
        total = ax_tree.count_refs(tree)  # type: ignore[arg-type]
        kept = ax_tree.count_refs(pruned)
        assert kept < total
        text = "\n".join(ax_tree.render(pruned))
        assert '"Password"' in text and "[secure]" in text
        assert '"Sign in"' in text

    def test_index_sees_folded_nodes(self) -> None:
        tree = fixture("snapshot_editor.json")["data"]["tree"]  # type: ignore[index]
        assert len(ax_tree.index(tree)) == ax_tree.count_refs(tree)  # type: ignore[arg-type]

    def test_cap_says_what_was_cut(self) -> None:
        text, cut = ax_tree.cap([f"line {i}" for i in range(100)], 60)
        assert cut and "more lines cut" in text and len(text) < 120

    def test_state_text_has_no_refs(self) -> None:
        state = ax_tree.state_text(fixture("snapshot_form.json")["data"], 2000)  # type: ignore[arg-type]
        assert state is not None
        assert state.startswith('window "Test Form"')
        assert "@s3" not in state
        assert '"Remember me"' in state
