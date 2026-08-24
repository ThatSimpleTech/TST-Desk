"""The allowlist as the daemon actually wires it (TD-606).

Every run in `test_shell_tools.py` passes `allowed_commands` to
`register_builtin_handlers` by hand, so none of them crosses the seam where
the defect lived: the config says `[]`, the daemon turns that into `()`, and
`ShellPolicy` reads a present-but-empty allowlist and refuses everything.
`()` is not `None`, and `None` is the unrestricted sentinel.

So these runs start from a `.tst/config.yaml` on disk — or its absence — and
go through `load_workspace_boundary` and the same accessor the daemon calls.
Nothing here constructs a `ShellPolicy` or names `allowed_commands` directly;
that is the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_dispatch import attach_auto_approver, make_classifier
from tstd.autonomy import Boundary
from tstd.boundary_config import load_workspace_boundary
from tstd.session import Session
from tstd.tools import ToolDispatcher, create_registry, register_builtin_handlers
from tstd.tools.boundary import PathGuard


def _write_config(workspace: Path, body: str) -> None:
    """Plant a real `.tst/config.yaml`, the way a user would."""
    path = workspace / ".tst" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _dispatcher_as_the_daemon_builds_it(workspace: Path) -> ToolDispatcher:
    """Wire the shell tool from the workspace's own config.

    Mirrors `Daemon._build_session`: load the boundary off disk, ask it for
    the shell allowlist, hand that to `register_builtin_handlers`. The
    allowlist value is never chosen here — that is what makes this a test of
    the wiring rather than of `ShellPolicy`.
    """
    boundary_config = load_workspace_boundary(workspace)
    boundary = Boundary(workspace_root=workspace)
    dispatcher = attach_auto_approver(
        ToolDispatcher(
            create_registry(),
            classifier=make_classifier(str(workspace)),
            path_guard=PathGuard(boundary),
        )
    )
    register_builtin_handlers(
        dispatcher,
        allowed_commands=boundary_config.boundary.shell_allowlist(),
    )
    return dispatcher


async def _run(workspace: Path, command: str) -> tuple[str, str]:
    dispatcher = _dispatcher_as_the_daemon_builds_it(workspace)
    result = await dispatcher.dispatch("c1", "shell", {"command": command}, Session(str(workspace)))
    return result.status, result.output


class TestUnrestrictedByDefault:
    async def test_a_workspace_with_no_config_can_run_a_command(self, tmp_path: Path) -> None:
        """The default state of every new workspace."""
        assert not (tmp_path / ".tst").exists()

        status, output = await _run(tmp_path, "echo hi")

        assert status == "success", output
        assert "hi" in output

    async def test_an_explicitly_empty_allowlist_is_unrestricted(self, tmp_path: Path) -> None:
        """What the shipped template has always claimed: empty means any."""
        _write_config(tmp_path, "boundary:\n  allowed_commands: []\n")

        status, output = await _run(tmp_path, "echo hi")

        assert status == "success", output
        assert "hi" in output

    async def test_a_config_that_omits_the_key_is_unrestricted(self, tmp_path: Path) -> None:
        """A `.tst/config.yaml` that configures something else entirely."""
        _write_config(tmp_path, 'boundary:\n  writable_paths:\n    - "**"\n')

        status, output = await _run(tmp_path, "echo hi")

        assert status == "success", output


class TestAPopulatedAllowlistStillRestricts:
    """The negative half.

    A fix that satisfied the criteria above by disabling the allowlist
    outright would pass every run in the class above. These are what stop
    that.
    """

    async def test_a_listed_binary_runs(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "boundary:\n  allowed_commands: [echo]\n")

        status, output = await _run(tmp_path, "echo hi")

        assert status == "success", output
        assert "hi" in output

    async def test_an_unlisted_binary_is_refused(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "boundary:\n  allowed_commands: [echo]\n")

        # `ls` is not a stock Windows binary; this interpreter is, and
        # it is never named `echo`.
        status, output = await _run(tmp_path, f'"{sys.executable}" -c pass')

        assert status == "error"
        assert "not in allowed_commands" in output

    async def test_every_segment_of_a_pipeline_is_checked(self, tmp_path: Path) -> None:
        """An allowlist that only guards the first binary is not a wall."""
        _write_config(tmp_path, "boundary:\n  allowed_commands: [echo]\n")

        status, output = await _run(tmp_path, f'echo hi | "{sys.executable}" -c pass')

        assert status == "error"
        assert "not in allowed_commands" in output


class TestTheDaemonsOwnWiring:
    """What the daemon actually hands the shell tool.

    The runs above prove the *semantics*, but they build their own
    dispatcher — which is the same shape of gap that let this defect ship, so
    reverting `daemon.py` leaves them green. These watch the daemon call the
    seam itself, so they go red the moment it goes back to passing the raw
    list. The dispatcher is captured in a closure handed to `agent_loop`, so
    the seam is the only place to observe it from.
    """

    @staticmethod
    async def _allowlist_the_daemon_passes(
        workspace: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[str, ...] | None:
        import tstd.daemon as daemon_mod

        seen: list[tuple[str, ...] | None] = []
        real = daemon_mod.register_builtin_handlers

        def _spy(
            dispatcher: object,
            allowed_commands: tuple[str, ...] | None = None,
            **kwargs: object,
        ) -> None:
            seen.append(allowed_commands)
            real(dispatcher, allowed_commands=allowed_commands, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(daemon_mod, "register_builtin_handlers", _spy)

        daemon = daemon_mod.Daemon(data_dir=data_dir)
        await daemon._start_session(str(workspace))
        assert seen, "the daemon never registered the builtin handlers"
        return seen[-1]

    async def test_no_config_hands_over_the_unrestricted_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()

        passed = await self._allowlist_the_daemon_passes(workspace, tmp_path / "data", monkeypatch)

        assert passed is None

    async def test_an_empty_list_hands_over_the_unrestricted_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_config(workspace, "boundary:\n  allowed_commands: []\n")

        passed = await self._allowlist_the_daemon_passes(workspace, tmp_path / "data", monkeypatch)

        assert passed is None

    async def test_a_populated_list_is_handed_over_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The allowlist must survive the fix, not be switched off by it."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_config(workspace, "boundary:\n  allowed_commands: [echo, cat]\n")

        passed = await self._allowlist_the_daemon_passes(workspace, tmp_path / "data", monkeypatch)

        assert passed == ("echo", "cat")


class TestTheAccessorItself:
    """`shell_allowlist()` owns the empty-means-unrestricted rule.

    Kept as unit checks so a future caller that skips the accessor and reads
    `allowed_commands` directly is a visible mistake rather than a silent one.
    """

    @pytest.mark.parametrize("body", ["", "boundary:\n  allowed_commands: []\n"])
    def test_empty_or_absent_yields_the_unrestricted_sentinel(
        self, tmp_path: Path, body: str
    ) -> None:
        if body:
            _write_config(tmp_path, body)

        assert load_workspace_boundary(tmp_path).boundary.shell_allowlist() is None

    def test_a_populated_list_yields_a_tuple(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "boundary:\n  allowed_commands: [echo, cat]\n")

        assert load_workspace_boundary(tmp_path).boundary.shell_allowlist() == ("echo", "cat")
