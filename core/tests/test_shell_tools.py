"""Tests for the shell tool (TD-605).

Covers the acceptance criteria:

1. Commands run with the workspace as working directory.
2. Configurable timeout; process group killed on timeout or cancel.
3. stdout and stderr streamed to the timeline as they arrive, not
   buffered to the end.
4. Output capped with truncation markers.
5. Exit code returned; non-zero is a normal result the model can reason
   about, not an error.
6. Environment sanitized: no API keys, no keychain material, no tokens
   passed to child processes — asserted by test.
7. ``allowed_commands`` allowlist enforced when configured, matching on
   the resolved binary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_dispatch import (
    attach_auto_approver,
    make_config,
    start_loop,
    wait_for_turn,
)
from tstd.autonomy import AmbiguousClassifier, Boundary, DecisionClassifier
from tstd.mock import MockProvider, Script
from tstd.protocol import ShellOutput
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import (
    ShellPolicy,
    ToolDispatcher,
    create_registry,
    register_builtin_handlers,
    run_shell,
)
from tstd.tools.boundary import PathGuard
from tstd.tools.shell import sanitized_env

# A command that names its own process group, then parks an escapee in it:
# the shell (the group leader) writes its pid — the test's spawn-ack and
# probe handle — then backgrounds a subshell that writes a marker only if
# it is still alive when the test *releases* it.  The writer is gated on
# ``release.txt``, not on ``sleep N`` (TD-1409): a delayed kill cannot
# lose a race against a wall clock, because the grandchild never writes
# unless the test says so — and the test never says so until the group
# is gone (or the kill was refused).
_ESCAPE_PROBE_CMD = (
    "echo $$ > pgid.txt; "
    "{ while [ ! -f release.txt ]; do sleep 0.05; done; touch kicked.txt; } & "
    "wait"
)

# macOS intermittently vetoes same-uid process-group kills with EPERM:
# the refusal attaches to the group and no userspace retry or external
# kill breaks it, so the command survives the whole test.  The product
# reports the refusal (result header or log) instead of claiming the
# kill; only then are group-death assertions vacuous.
_KILL_REFUSED = "kill refused"

# TD-1406 gave Windows a tree kill — CREATE_NEW_PROCESS_GROUP at the spawn
# and `taskkill /T /F` in `_kill_process_group` — but no Windows run has
# executed it, and a process-tree kill means nothing until a real OS has
# been asked to perform one.  The escape probe is also POSIX shell (`$$`,
# `&`, `wait`), so unskipping needs a cmd/PowerShell equivalent as well as
# a green Windows leg.  Kept a skip rather than a passing assertion so the
# suite does not claim a guarantee nobody has observed.
requires_posix_process_group = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "TD-1406: the Windows tree kill (CREATE_NEW_PROCESS_GROUP + taskkill /T /F) is "
        "implemented but unverified — no Windows run has exercised it, and the escape "
        "probe is a POSIX shell command; unskip once a Windows leg confirms both"
    ),
)


async def _wait_for_file(path: Path, seconds: float = 5.0) -> None:
    """Poll for *path* to appear — the condition a fixed sleep approximates.

    Failing to appear within *seconds* means the command never started:
    a product failure worth failing the test for, not a flake to absorb.
    """
    deadline = asyncio.get_running_loop().time() + seconds
    while not await asyncio.to_thread(path.exists):
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"{path.name} never appeared — the command did not start")
        await asyncio.sleep(0.02)


async def _assert_group_gone(tmp_path: Path, seconds: float = 5.0) -> None:
    """Assert the killed process group is gone (TD-1407).

    The condition the old kicked.txt + ``sleep 2`` marker approximated: a
    surviving group IS the escaped grandchild this battery exists to
    catch, so the probe cannot become a test that cannot fail.  Only ever
    called after the product reported the kill delivered — a veto round
    (refusal reported) returns before probing, because the vetoed group
    outlives the test.

    A kill landing before the leader's first write leaves no pgid file;
    nothing was ever forked, so nothing could escape — the assertion is
    vacuous and passes by waiting the file out.
    """
    pgid_file = tmp_path / "pgid.txt"
    deadline = asyncio.get_running_loop().time() + seconds
    while not await asyncio.to_thread(pgid_file.exists):
        if asyncio.get_running_loop().time() > deadline:
            return
        await asyncio.sleep(0.02)
    pgid = int((await asyncio.to_thread(pgid_file.read_text)).strip())
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # Probing is still kill(2): a vetoed group reads alive.  We
            # only probe on a reported-delivered kill, so this is out of
            # model — treat it as alive and let the deadline decide.
            pass
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"process group {pgid} survived the reported kill — a grandchild escaped")
        await asyncio.sleep(0.02)


def _python(code: str) -> str:
    """A shell command running *code* under this interpreter, cross-platform.

    *code* must use single quotes only: the command line is wrapped in
    double quotes, which both cmd.exe and POSIX sh accept.
    """
    return f'"{sys.executable}" -c "{code}"'


async def _stub_worker(prompt: str) -> str:
    return "B"


def make_session(workspace: Path) -> Session:
    return Session(str(workspace))


def make_shell_dispatcher(
    workspace: Path, allowed_commands: tuple[str, ...] | None = None
) -> ToolDispatcher:
    """A dispatcher with classifier + path guard + builtin handlers."""
    boundary = Boundary(workspace_root=workspace)
    dispatcher = attach_auto_approver(  # TD-802: mechanics tests auto-approve
        ToolDispatcher(
            create_registry(),
            classifier=AmbiguousClassifier(
                static=DecisionClassifier(boundary),
                call_worker=_stub_worker,
            ),
            path_guard=PathGuard(boundary),
        )
    )
    register_builtin_handlers(dispatcher, allowed_commands=allowed_commands)
    return dispatcher


# ── AC1: workspace as working directory ────────────────────────────────


class TestWorkspaceCwd:
    async def test_pwd_is_the_workspace(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "c1",
            "shell",
            {"command": _python("import os; print(os.path.realpath(os.getcwd()))")},
            session,
        )
        assert result.status == "success"
        # Normalize both sides: on Windows the child's getcwd may differ
        # from pytest's tmp_path string in case or 8.3 form.  (tmp_path
        # is already symlink-resolved; normcase is a pure string op.)
        expected = os.path.normcase(str(tmp_path))
        assert expected in os.path.normcase(result.output)

    async def test_relative_paths_resolve_in_workspace(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("found it\n")
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "shell", {"command": "cat marker.txt"}, session)
        assert result.status == "success"
        assert "found it" in result.output


# ── AC2: timeout + process-group kill ───────────────────────────────────


class TestTimeoutAndGroupKill:
    @requires_posix_process_group
    async def test_timeout_kills_process_group(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        start = asyncio.get_running_loop().time()
        result = await dispatcher.dispatch(
            "c1", "shell", {"command": _ESCAPE_PROBE_CMD, "timeout_secs": 1}, session
        )
        elapsed = asyncio.get_running_loop().time() - start
        assert result.status == "success"
        if _KILL_REFUSED in result.output:
            # The OS vetoed the kill; the refusal is reported and the
            # command ran out — timing and group assertions are vacuous.
            return
        assert "timed out after 1s — process group killed" in result.output
        # The timeout fired long before the command's natural 30s end; a
        # generous bound, because the loop's own stalls count toward it.
        assert elapsed < 25
        # The kill was reported delivered: the whole group — the parked
        # subshell included — must be gone (TD-1407).
        await _assert_group_gone(tmp_path)

    @pytest.mark.parametrize("bad_timeout", [0, -5])
    async def test_nonpositive_timeout_refused(self, tmp_path: Path, bad_timeout: int) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "c1", "shell", {"command": "echo hi", "timeout_secs": bad_timeout}, session
        )
        assert result.status == "error"
        assert "positive integer" in result.output

    async def test_missing_workspace_refused(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path / "no-such-dir"))
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "shell", {"command": "echo hi"}, session)
        assert result.status == "error"
        assert "does not exist" in result.output


class TestCancel:
    @requires_posix_process_group
    async def test_cancel_kills_process_group(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        task = asyncio.create_task(
            dispatcher.dispatch("c1", "shell", {"command": _ESCAPE_PROBE_CMD}, session)
        )
        # Cancel only once the group provably exists — its leader wrote
        # its pgid — so this always exercises the mid-run cancel path.
        await _wait_for_file(tmp_path / "pgid.txt")
        await session.cancel()
        result = await task
        assert result.status == "success"
        if _KILL_REFUSED in result.output:
            # The OS vetoed the kill; the refusal is reported and the
            # command ran out — the group assertion is vacuous.
            return
        assert "cancelled — process group killed" in result.output
        await _assert_group_gone(tmp_path)

    async def test_cancel_before_start_does_not_run(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        await session.cancel()
        result = await dispatcher.dispatch("c1", "shell", {"command": "echo hi"}, session)
        assert result.status == "success"
        assert result.output == "cancelled — command not started"

    @requires_posix_process_group
    async def test_cancelled_error_path_kills_group(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # SessionRunner.cancel() cancels the loop task outright; the
        # handler's CancelledError path must kill the group before the
        # cancellation propagates.
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        task = asyncio.create_task(
            dispatcher.dispatch("c1", "shell", {"command": _ESCAPE_PROBE_CMD}, session)
        )
        # Cancel only once the group provably exists — never mid-spawn,
        # which is the next test's window.
        await _wait_for_file(tmp_path / "pgid.txt")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if any(_KILL_REFUSED in r.getMessage() for r in caplog.records):
            return  # the OS vetoed the kill; the command ran out
        await _assert_group_gone(tmp_path)

    @requires_posix_process_group
    async def test_escape_writer_does_not_fire_on_a_clock(self, tmp_path: Path) -> None:
        """TD-1409: a ``sleep 2; touch kicked`` writer would produce the
        marker during a 3s delayed kill.  The release-gated writer must
        not — this is the reproduction the old arithmetic lost to."""
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        task = asyncio.create_task(
            dispatcher.dispatch("c1", "shell", {"command": _ESCAPE_PROBE_CMD}, session)
        )
        await _wait_for_file(tmp_path / "pgid.txt")
        # The old race window, plus margin.  If the writer is still a
        # sleep, kicked.txt appears here and this assertion is the red.
        await asyncio.sleep(3.0)
        assert not (tmp_path / "kicked.txt").exists(), (
            "escapee wrote kicked.txt without release.txt — the writer "
            "is still a wall clock (TD-1409)"
        )
        await session.cancel()
        result = await task
        if _KILL_REFUSED in result.output:
            return
        await _assert_group_gone(tmp_path)
        # Releasing after the group is gone cannot raise the dead.
        (tmp_path / "release.txt").write_text("", encoding="utf-8")
        await asyncio.sleep(0.2)
        assert not (tmp_path / "kicked.txt").exists()

    @requires_posix_process_group
    async def test_cancel_during_spawn_kills_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A cancellation landing mid-spawn must still kill the group: the
        # spawn is shielded, the handler waits for it to settle, and the
        # child dies instead of escaping as an orphan (the flake family
        # above traced to this window).
        entered = asyncio.Event()
        real_spawn = asyncio.create_subprocess_shell

        async def _spy(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            entered.set()
            return await real_spawn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_shell", _spy)
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        task = asyncio.create_task(
            dispatcher.dispatch("c1", "shell", {"command": _ESCAPE_PROBE_CMD}, session)
        )
        # Cancel only once the handler is provably inside the spawn.
        await entered.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if any(_KILL_REFUSED in r.getMessage() for r in caplog.records):
            return  # the OS vetoed the kill; the command ran out
        await _assert_group_gone(tmp_path)

    @requires_posix_process_group
    async def test_kill_refusal_reported_in_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the OS vetoes the group kill (EPERM), the result must say
        # so instead of claiming the group died.
        def _refusing_killpg(pgid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "killpg", _refusing_killpg)
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        task = asyncio.create_task(
            dispatcher.dispatch("c1", "shell", {"command": _ESCAPE_PROBE_CMD}, session)
        )
        await asyncio.sleep(0.3)
        await session.cancel()
        result = await task
        assert result.status == "success"
        assert _KILL_REFUSED in result.output
        assert "process group killed" not in result.output

    @requires_posix_process_group
    async def test_kill_refusal_on_task_cancel_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The CancelledError path has no result to carry the refusal, so
        # it is logged instead.
        def _refusing_killpg(pgid: int, sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "killpg", _refusing_killpg)
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        task = asyncio.create_task(
            dispatcher.dispatch("c1", "shell", {"command": _ESCAPE_PROBE_CMD}, session)
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert any(_KILL_REFUSED in r.getMessage() for r in caplog.records)


# ── AC3: streamed to the timeline as it arrives ────────────────────────


class TestStreaming:
    async def test_output_arrives_while_command_runs(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        command = _python(
            "import time; print('first', flush=True); time.sleep(0.6); print('second')"
        )
        task = asyncio.create_task(
            dispatcher.dispatch("call_stream", "shell", {"command": command}, session)
        )
        # The first chunk lands in the event log while the task is still
        # pending — streamed, not buffered to the end.
        deadline = asyncio.get_running_loop().time() + 5.0
        streamed_live = False
        while asyncio.get_running_loop().time() < deadline:
            events = [e for e in session.event_log.all_events if isinstance(e, ShellOutput)]
            if events and not task.done():
                streamed_live = True
                break
            await asyncio.sleep(0.02)
        assert streamed_live

        result = await task
        assert result.status == "success"
        chunks = [e for e in session.event_log.all_events if isinstance(e, ShellOutput)]
        stdout = "".join(e.chunk for e in chunks if e.stream == "stdout")
        # Line endings are the child's platform's (\r\n on Windows).
        assert stdout.replace("\r\n", "\n") == "first\nsecond\n"
        assert all(e.tool_call_id == "call_stream" for e in chunks)
        assert all(e.session_id == session.id for e in chunks)

    async def test_stderr_streams_in_its_own_lane(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "c1",
            "shell",
            {"command": _python("import sys; print('oops', file=sys.stderr)")},
            session,
        )
        assert result.status == "success"
        err_chunks = [
            e
            for e in session.event_log.all_events
            if isinstance(e, ShellOutput) and e.stream == "stderr"
        ]
        assert "".join(e.chunk for e in err_chunks).replace("\r\n", "\n") == "oops\n"
        assert "── stdout ──\n(empty)" in result.output


# ── AC4: capped with truncation markers ────────────────────────────────


class TestOutputCap:
    async def test_stream_capped_with_marker(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        result = await run_shell(
            session,
            "head -c 1000 /dev/zero | tr '\\0' a",
            timeout_secs=10,
            tool_call_id="tc-cap",
            policy=ShellPolicy(max_stream_chars=200),
        )
        assert "exit code: 0" in result
        stdout_section = result.split("── stdout ──\n")[1].split("\n── stderr ──")[0]
        assert "a" * 200 in stdout_section
        assert "a" * 201 not in stdout_section
        assert "… [truncated: stream output exceeds cap]" in stdout_section
        # Nothing is emitted to the timeline past the cap either.
        events = [e for e in session.event_log.all_events if isinstance(e, ShellOutput)]
        assert sum(len(e.chunk) for e in events) <= 200


# ── AC5: exit code is a result, not an error ───────────────────────────


class TestExitCode:
    async def test_nonzero_exit_is_a_normal_result(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "shell", {"command": "exit 3"}, session)
        assert result.status == "success"
        assert "exit code: 3" in result.output

    async def test_zero_exit(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "shell", {"command": "exit 0"}, session)
        assert result.status == "success"
        assert "exit code: 0" in result.output

    async def test_stderr_lands_in_its_section(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch(
            "c1",
            "shell",
            {"command": _python("import sys; print('oops', file=sys.stderr); sys.exit(2)")},
            session,
        )
        assert result.status == "success"
        assert "exit code: 2" in result.output
        assert "── stderr ──\noops" in result.output


# ── AC6: environment sanitized ─────────────────────────────────────────


class TestEnvSanitized:
    async def test_child_env_excludes_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-123")  # tst-secret-ok
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_value")
        monkeypatch.setenv("DB_PASSWORD", "hunter2-value")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value")
        monkeypatch.setenv("TST_NORMAL_VAR", "visible-value")

        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "shell", {"command": "env"}, session)
        assert result.status == "success"
        for leaked in (
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "DB_PASSWORD",
            "AWS_SECRET_ACCESS_KEY",
            "sk-super-secret-123",  # tst-secret-ok
            "ghp_secret_value",
            "hunter2-value",
            "aws-secret-value",
        ):
            assert leaked not in result.output
        assert "TST_NORMAL_VAR=visible-value" in result.output
        assert "PATH=" in result.output

    def test_sanitized_env_filters_secret_shapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("MY_TOKEN", "t")
        monkeypatch.setenv("SOME_CREDENTIAL", "c")
        monkeypatch.setenv("PLAIN_VAR", "keep")
        env = sanitized_env()
        assert "OPENAI_API_KEY" not in env
        assert "MY_TOKEN" not in env
        assert "SOME_CREDENTIAL" not in env
        assert env["PLAIN_VAR"] == "keep"
        assert "PATH" in env


# ── AC7: allowed_commands allowlist ────────────────────────────────────


class TestAllowlist:
    async def test_allowed_binary_runs(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo", "cat"))
        result = await dispatcher.dispatch("c1", "shell", {"command": "echo hi"}, session)
        assert result.status == "success"
        assert "hi" in result.output

    async def test_pipeline_of_allowed_binaries_runs(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo", "cat"))
        result = await dispatcher.dispatch("c1", "shell", {"command": "echo hi | cat"}, session)
        assert result.status == "success"
        assert "hi" in result.output

    async def test_disallowed_binary_refused(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo", "cat"))
        result = await dispatcher.dispatch("c1", "shell", {"command": "ls"}, session)
        assert result.status == "error"
        assert "not in allowed_commands" in result.output

    async def test_every_segment_is_checked(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo", "cat"))
        result = await dispatcher.dispatch("c1", "shell", {"command": "echo a && ls"}, session)
        assert result.status == "error"
        assert "not in allowed_commands" in result.output

    async def test_unresolvable_binary_refused(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo",))
        result = await dispatcher.dispatch(
            "c1", "shell", {"command": "definitely_not_a_real_binary_605"}, session
        )
        assert result.status == "error"
        assert "cannot resolve" in result.output

    async def test_wrapper_matched_as_itself(self, tmp_path: Path) -> None:
        # Wrappers are not pierced: `sh` is the segment's binary and is
        # not on the list, so the call is refused.
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo",))
        result = await dispatcher.dispatch("c1", "shell", {"command": "sh -c 'echo hi'"}, session)
        assert result.status == "error"
        assert "not in allowed_commands" in result.output

    async def test_backtick_substitution_refused(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo",))
        result = await dispatcher.dispatch("c1", "shell", {"command": "echo `ls`"}, session)
        assert result.status == "error"
        assert "backtick" in result.output

    async def test_absolute_path_resolves_to_basename(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        # This interpreter is an absolute path that exists on every
        # platform; the allowlist entry is the basename it must resolve
        # to (extension-stripped and lowercased on Windows, matching
        # check_allowed).  "-c pass" keeps the command free of the shell
        # metacharacters the allowlist parser splits segments on.
        binary = sys.executable
        name = Path(binary).stem.lower() if sys.platform == "win32" else Path(binary).name
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=(name,))
        result = await dispatcher.dispatch(
            "c1", "shell", {"command": f'"{binary}" -c pass'}, session
        )
        assert result.status == "success"
        assert "exit code: 0" in result.output

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "TD-1406: VAR=value assignment prefixes are POSIX shell syntax; "
            "the daemon's shell on Windows is cmd.exe"
        ),
    )
    async def test_env_assignment_prefix_skipped(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path, allowed_commands=("echo",))
        result = await dispatcher.dispatch("c1", "shell", {"command": "FOO=1 echo hi"}, session)
        assert result.status == "success"
        assert "hi" in result.output

    async def test_unrestricted_by_default(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        dispatcher = make_shell_dispatcher(tmp_path)
        result = await dispatcher.dispatch("c1", "shell", {"command": "ls"}, session)
        assert result.status == "success"


# ── Handler-level guards ───────────────────────────────────────────────


class TestGuards:
    async def test_requires_a_session(self) -> None:
        with pytest.raises(ValueError, match="active session"):
            await run_shell(None, "echo hi")

    async def test_rejects_nonpositive_timeout_directly(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        with pytest.raises(ValueError, match="positive integer"):
            await run_shell(session, "echo hi", timeout_secs=0)


# ── Loop integration ───────────────────────────────────────────────────


class TestLoopIntegration:
    async def test_shell_call_streams_and_reports_through_the_loop(self, tmp_path: Path) -> None:
        session = make_session(tmp_path)
        router = TierRouter(lead_turns=3)
        config = make_config()
        registry = create_registry()
        dispatcher = make_shell_dispatcher(tmp_path)

        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="shell",
                        tool_arguments='{"command": "echo hello"}',
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, registry, dispatcher)
        await session.add_user_message("Run the echo command")
        await wait_for_turn(session, 1)

        events = session.event_log.all_events
        tool_calls = [e for e in events if isinstance(e, ToolCallEvent) and e.name == "shell"]
        outputs = [e for e in events if isinstance(e, ShellOutput)]
        results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_calls) == 1
        assert outputs, "shell output must stream to the timeline"
        assert len(results) == 1
        # Timeline order: the call, then its streamed output, then the result.
        assert tool_calls[0].seq < outputs[0].seq < results[0].seq
        assert results[0].status == "success"
        assert "hello" in results[0].output

        await runner.cancel()
