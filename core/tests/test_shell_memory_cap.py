"""Memory cap on shell commands (TD-5116).

Acceptance criteria:

1. ``caps.shell_memory_mb`` (default about half of RAM) applies through a
   cgroup scope where systemd user scopes exist, otherwise through
   ``RLIMIT_AS``. Podman runs get ``--memory``.
2. A command over the cap is killed. The tool result says so plainly.
3. A memory hog is killed and the daemon survives.

The scope tests need a systemd user manager with the memory controller
(EZER has one) and skip without it; the ``RLIMIT_AS`` tests force that
path, so CI runs them either way. Every hog stops on its own at 2 GiB, so
a cap that fails to apply fails the test instead of the machine.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_dispatch import make_config
from tests.test_sandbox import _fake_runtime, _which_for
from tstd.autonomy.charter import Charter
from tstd.autonomy.sandbox import autonomy_shell_argv, sandbox_exec
from tstd.boundary_config import BoundaryConfig, boundary_from_mapping
from tstd.config import ConfigError
from tstd.daemon import Daemon
from tstd.host_memory import MIB, default_shell_memory_mb, physical_memory_mb
from tstd.mock import MockProvider, Script
from tstd.protocol import ApprovalRequest, ToolResult, TurnComplete
from tstd.session import Session
from tstd.tools import run_shell, shell_memory
from tstd.tools.shell import sanitized_env
from tstd.tools.shell_memory import ShellMemoryCap, cap_note, plan_cap, ran_out_of_memory
from tstd.workspace_trust import find_loosened

requires_linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux memory caps")

# Touches every page (``b'x' * n``, not a lazily zeroed buffer), so resident
# memory grows with it; stops by itself at 2 GiB.
_HOG = (
    f'"{sys.executable}" -c "'
    "blocks = [b'x' * (8 << 20) for _ in range(256)]; print('survived')"
    '"'
)


@pytest.fixture(autouse=True)
def _fresh_probe() -> Iterator[None]:
    """No test inherits, or leaves behind, another test's probe answer."""
    shell_memory.reset_scope_probe()
    yield
    shell_memory.reset_scope_probe()


@pytest.fixture
def rlimit_only(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_scopes(_env: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(shell_memory, "_probe_scope_tools", _no_scopes)


@pytest.fixture
async def scopes() -> None:
    if sys.platform != "linux":
        pytest.skip("systemd user scopes are Linux only")
    if await shell_memory._probe_scope_tools(sanitized_env()) is None:
        pytest.skip("no systemd user scope with the memory controller here")


def _session(workspace: Path, mb: int) -> Session:
    session = Session(str(workspace))
    session.boundary_config.caps.shell_memory_mb = mb
    return session


# ── AC1: the key, its default and the trust gate ────────────────────────


class TestConfig:
    def test_unset_is_half_of_ram(self) -> None:
        total = physical_memory_mb()
        assert total is not None
        assert BoundaryConfig().caps.shell_memory_mb is None
        assert BoundaryConfig().caps.shell_memory_cap() == total // 2 == default_shell_memory_mb()

    def test_a_set_value_wins(self, tmp_path: Path) -> None:
        cfg = boundary_from_mapping(tmp_path, {"caps": {"shell_memory_mb": 512}})
        assert cfg.caps.shell_memory_cap() == 512

    @pytest.mark.parametrize("value", [0, 63, -1, "lots"])
    def test_a_bad_value_is_a_load_error_naming_the_key(self, tmp_path: Path, value: Any) -> None:
        with pytest.raises(ConfigError, match=r"caps\.shell_memory_mb"):
            boundary_from_mapping(tmp_path, {"caps": {"shell_memory_mb": value}})

    def test_a_charter_may_set_it(self) -> None:
        charter = Charter.model_validate(
            {
                "objective": "build",
                "definition_of_done": ["tests pass"],
                "boundary": {},
                "caps": {"shell_memory_mb": 2048},
            }
        )
        assert charter.caps.shell_memory_cap() == 2048

    def test_raising_it_above_the_default_waits_for_trust(self, tmp_path: Path) -> None:
        default = default_shell_memory_mb()
        assert default is not None
        raised = find_loosened(tmp_path, {"caps": {"shell_memory_mb": default + 1}})
        assert [(i.key, i.value) for i in raised] == [("caps.shell_memory_mb", str(default + 1))]
        assert "half of RAM" in raised[0].default

    @pytest.mark.parametrize("mapping", [{"caps": {"shell_memory_mb": 64}}, {"caps": {}}, {}])
    def test_lowering_or_leaving_it_never_asks(
        self, tmp_path: Path, mapping: dict[str, Any]
    ) -> None:
        assert find_loosened(tmp_path, mapping) == []


# ── AC1: Podman runs get --memory ───────────────────────────────────────


class TestContainer:
    def test_the_shell_argv_carries_the_cap_before_the_image(self, tmp_path: Path) -> None:
        fake = _fake_runtime(tmp_path)
        argv = autonomy_shell_argv(
            tmp_path, "true", runtime="podman", image="alpine", which=_which_for(fake), memory_mb=96
        )
        assert "--memory=96m" in argv
        assert argv.index("--memory=96m") < argv.index("alpine")

    def test_unset_is_half_of_ram(self, tmp_path: Path) -> None:
        fake = _fake_runtime(tmp_path)
        argv = autonomy_shell_argv(
            tmp_path, "true", runtime="podman", image="alpine", which=_which_for(fake)
        )
        assert f"--memory={default_shell_memory_mb()}m" in argv

    async def test_sandbox_exec_carries_it_too(self, tmp_path: Path) -> None:
        fake = _fake_runtime(tmp_path)

        async def _rootless(_binary: str) -> Any:
            from tstd.autonomy.sandbox import RuntimeState

            return RuntimeState.ROOTLESS

        result = await sandbox_exec(
            tmp_path, ("true",), config=make_config(), which=_which_for(fake), inspect=_rootless
        )
        assert f"--memory={default_shell_memory_mb()}m" in result.argv

    @requires_linux
    async def test_an_autonomous_command_runs_under_the_session_cap(self, tmp_path: Path) -> None:
        # A runtime that records its argv and exits as a killed container does.
        log = tmp_path / "argv.json"
        runtime = tmp_path / "podman"
        runtime.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\nraise SystemExit(137)\n",
            encoding="utf-8",
        )
        runtime.chmod(0o755)
        ws = tmp_path / "ws"
        ws.mkdir()
        session = _session(ws, 96)
        session.autonomy = True
        session.config = make_config()
        session.config.autonomy.runtime = str(runtime)
        result = await run_shell(session, "true")
        assert "--memory=96m" in json.loads(log.read_text(encoding="utf-8"))
        assert "exit code: 137" in result
        assert "memory cap: killed (exit 137) in a container capped at 96 MB" in result

    async def test_only_a_sigkill_exit_is_put_down_to_the_cap(self) -> None:
        cap = ShellMemoryCap(mb=96, method="container")
        assert await cap_note(cap, 1, "MemoryError") == ""
        assert await cap_note(cap, 0, "") == ""


# ── AC1 + AC2: RLIMIT_AS where there are no user scopes ─────────────────


@requires_linux
@pytest.mark.usefixtures("rlimit_only")
class TestRlimit:
    async def test_a_hog_is_stopped_and_the_result_says_so(self, tmp_path: Path) -> None:
        result = await run_shell(_session(tmp_path, 128), _HOG, timeout_secs=60)
        assert "survived" not in result
        assert "exit code: 0" not in result
        assert (
            "memory cap: the command ran out of memory at its 128 MB cap (caps.shell_memory_mb)"
            in result
        )

    async def test_the_limit_is_inherited_and_cannot_be_raised(self, tmp_path: Path) -> None:
        result = await run_shell(
            _session(tmp_path, 128), "ulimit -v; ulimit -H -v; ulimit -v unlimited; echo rc=$?"
        )
        lines = result.split("── stdout ──")[1].split("── stderr ──")[0].split()
        assert lines[:2] == [str(128 * 1024)] * 2
        assert lines[2] != "rc=0"

    async def test_the_command_text_reaches_the_shell_unchanged(self, tmp_path: Path) -> None:
        command = "printf '%s|' \"a b\" 'c'\"'\"'d' \"$0\"; echo; exit 7"
        result = await run_shell(_session(tmp_path, 128), command)
        assert "a b|c'd|/bin/sh|" in result
        assert "exit code: 7" in result

    async def test_an_ordinary_failure_gets_no_note(self, tmp_path: Path) -> None:
        result = await run_shell(_session(tmp_path, 128), "echo nope >&2; exit 3")
        assert "exit code: 3" in result
        assert "memory cap" not in result

    async def test_a_lower_inherited_limit_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import resource

        hard = 100 * MIB
        monkeypatch.setattr(resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, hard))
        cap = await plan_cap(_session(Path("."), 512), sanitized_env())
        assert cap is not None
        assert (cap.method, cap.limit_kib, cap.mb) == ("rlimit", hard // 1024, 100)


class TestOutOfMemoryText:
    @pytest.mark.parametrize(
        "stderr",
        [
            "Traceback (most recent call last):\nMemoryError",
            "bash: fork: Cannot allocate memory",
            "terminate called after throwing an instance of 'std::bad_alloc'",
            "memory allocation of 1048576 bytes failed",
            "fatal error: runtime: out of memory",
            "Native memory allocation (mmap) failed to map 1 bytes",
        ],
    )
    def test_the_usual_runtimes_are_read(self, stderr: str) -> None:
        assert ran_out_of_memory(stderr)

    @pytest.mark.parametrize(
        "stderr",
        [
            "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
            "grep: nothing.txt: No such file or directory",
            "",
        ],
    )
    def test_a_runtime_heap_limit_or_other_error_is_not_the_cap(self, stderr: str) -> None:
        assert not ran_out_of_memory(stderr)


# ── AC1 + AC2: a systemd user scope where one exists ────────────────────


def _unit_loaded(unit: str) -> str:
    out = subprocess.run(
        ["systemctl", "--user", "show", "--property=LoadState", "--value", unit],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return out.stdout.strip()


@pytest.mark.usefixtures("scopes")
class TestScope:
    async def test_each_command_runs_in_its_own_capped_scope(self, tmp_path: Path) -> None:
        command = (
            "cg=$(sed -n 's/^0:://p' /proc/self/cgroup); echo \"$cg\"; "
            'cat "/sys/fs/cgroup$cg/memory.max" "/sys/fs/cgroup$cg/memory.swap.max"'
        )
        result = await run_shell(_session(tmp_path, 96), command)
        stdout = result.split("── stdout ──")[1].split("── stderr ──")[0].split()
        cgroup, memory_max, swap_max = stdout
        assert "/tstd-shell-" in cgroup and cgroup.endswith(".scope")
        # The daemon is not in it: the kernel kills inside the scope only.
        own = await asyncio.to_thread(Path("/proc/self/cgroup").read_text, encoding="ascii")
        assert cgroup not in own
        assert (memory_max, swap_max) == (str(96 * MIB), "0")

    async def test_a_hog_is_killed_and_the_result_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        units: list[str] = []
        real = shell_memory._unit_name

        def _record(kind: str) -> str:
            units.append(real(kind))
            return units[-1]

        monkeypatch.setattr(shell_memory, "_unit_name", _record)
        result = await run_shell(_session(tmp_path, 64), _HOG, timeout_secs=60)
        assert "survived" not in result
        assert (
            "memory cap: killed — the command went over its 64 MB memory cap "
            "(caps.shell_memory_mb)" in result
        )
        # The failed scope is cleared, not left in `systemctl --user --failed`.
        assert _unit_loaded(units[-1]) == "not-found"

    async def test_a_script_that_carries_on_after_the_kill_is_still_told(
        self, tmp_path: Path
    ) -> None:
        result = await run_shell(_session(tmp_path, 64), f"{_HOG}; echo carried-on", 60)
        assert "survived" not in result
        assert "memory cap: killed" in result

    async def test_an_ordinary_command_gets_no_note(self, tmp_path: Path) -> None:
        result = await run_shell(_session(tmp_path, 64), "echo fine; exit 2")
        assert "fine" in result and "exit code: 2" in result
        assert "memory cap" not in result

    async def test_the_command_text_reaches_the_shell_unexpanded(self, tmp_path: Path) -> None:
        # systemd 254+ rewrites ``$$`` and ``${x}`` in systemd-run arguments
        # unless told not to.
        command = 'x=kept; echo "[${x}]"; [ "$$" -gt 1 ] && echo pid-ok; printf \'%s|\' "$0"'
        result = await run_shell(_session(tmp_path, 64), command)
        assert "[kept]" in result
        assert "pid-ok" in result
        assert "/bin/sh|" in result


def _fake_systemd(tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A ``systemd-run`` that answers the probe the way *mode* says.

    ``old`` refuses ``--expand-environment`` and passes text through, as
    systemd before 254 does; ``expands`` takes the flag but loses the
    marker anyway; ``works`` passes.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemd-run").write_text(
        f"#!{sys.executable}\nimport sys\nmode = {mode!r}\n"
        "if mode == 'old' and '--expand-environment=no' in sys.argv:\n"
        "    sys.exit('systemd-run: unrecognized option')\n"
        "print('' if mode == 'expands' else 'verbatim')\n"
        f"print({256 * MIB})\n",
        encoding="utf-8",
    )
    (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return {"PATH": str(bin_dir)}


@requires_linux
class TestProbe:
    async def test_the_no_expand_flag_is_used_when_systemd_takes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools = await shell_memory._probe_scope_tools(_fake_systemd(tmp_path, "works", monkeypatch))
        assert tools is not None and tools.no_expand

    async def test_systemd_without_the_flag_runs_plain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools = await shell_memory._probe_scope_tools(_fake_systemd(tmp_path, "old", monkeypatch))
        assert tools is not None and not tools.no_expand
        cap = ShellMemoryCap(mb=64, method="scope", unit="u.scope", tools=tools)
        assert "--expand-environment" not in shell_memory.host_command(cap, "true")

    async def test_a_scope_that_mangles_the_command_is_not_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert (
            await shell_memory._probe_scope_tools(_fake_systemd(tmp_path, "expands", monkeypatch))
            is None
        )

    async def test_no_systemd_means_rlimit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        assert await shell_memory._probe_scope_tools({"PATH": str(tmp_path)}) is None
        cap = await plan_cap(_session(tmp_path, 64), {"PATH": str(tmp_path)})
        assert cap is not None and cap.method == "rlimit"


# ── Where the cap applies, and the probe ────────────────────────────────


class TestPlan:
    async def test_the_probe_runs_once_per_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        async def _counting(_env: dict[str, str]) -> None:
            calls.append(1)
            return None

        monkeypatch.setattr(shell_memory, "_probe_scope_tools", _counting)
        monkeypatch.setattr(shell_memory, "_is_linux", lambda: True)
        session = _session(Path("."), 256)
        for _ in range(3):
            await plan_cap(session, {})
        assert calls == [1]

    async def test_macos_and_windows_hosts_run_uncapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shell_memory, "_is_linux", lambda: False)
        assert await plan_cap(_session(Path("."), 256), {}) is None

    async def test_a_container_is_capped_on_every_platform(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shell_memory, "_is_linux", lambda: False)
        session = _session(Path("."), 256)
        session.autonomy = True
        assert await plan_cap(session, {}) == ShellMemoryCap(mb=256, method="container")


# ── AC3: the hog dies, the daemon carries on ────────────────────────────


@requires_linux
@pytest.mark.parametrize("method", ["best", "rlimit"])
async def test_a_hog_is_killed_and_the_daemon_survives(
    tmp_path: Path, method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real daemon: the hog's turn ends, the next call runs.

    ``best`` is whatever the machine has (a scope on EZER, ``RLIMIT_AS`` on
    a CI runner without a user manager); ``rlimit`` forces the fallback.
    """
    if method == "rlimit":

        async def _no_scopes(_env: dict[str, str]) -> None:
            return None

        monkeypatch.setattr(shell_memory, "_probe_scope_tools", _no_scopes)
    workspace = tmp_path / "ws"
    (workspace / ".tst").mkdir(parents=True)
    # Lower than the default, so it applies with nothing to trust.
    (workspace / ".tst" / "config.yaml").write_text(
        yaml.safe_dump({"caps": {"shell_memory_mb": 64}}), encoding="utf-8"
    )
    provider = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="shell",
                    tool_arguments=json.dumps({"command": _HOG}),
                ),
                Script(
                    kind="tool_call",
                    tool_name="shell",
                    tool_arguments=json.dumps({"command": "echo daemon-alive"}),
                ),
                Script(kind="stream", content="done"),
            ]
        }
    )
    daemon = Daemon(data_dir=tmp_path / "data", provider=provider)
    daemon.config = make_config()
    task = asyncio.create_task(daemon.run())

    async def send(msg: dict[str, Any]) -> dict[str, Any] | None:
        reply = await daemon._handle_message(json.dumps(msg), None)
        return None if reply is None else json.loads(reply)

    try:
        for _ in range(100):
            if daemon.ws_server.port:
                break
            await asyncio.sleep(0.05)
        opened = await send({"type": "open_workspace", "path": str(workspace)})
        assert opened is not None
        sid = str(opened["session_id"])
        session = daemon.session_registry.get(sid)
        assert session is not None
        await send({"type": "user_message", "session_id": sid, "content": "use memory"})

        # The mock reuses its call ids, so each card is answered by its seq.
        approved: set[int] = set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90
        while loop.time() < deadline:
            events = session.event_log.all_events
            if any(isinstance(e, TurnComplete) for e in events):
                break
            for event in events:
                if isinstance(event, ApprovalRequest) and event.seq not in approved:
                    approved.add(event.seq)
                    await send(
                        {"type": "approve", "session_id": sid, "tool_call_id": event.tool_call_id}
                    )
            await asyncio.sleep(0.05)

        results = [e for e in session.event_log.all_events if isinstance(e, ToolResult)]
        assert len(results) == 2, [r.output for r in results]
        assert "memory cap:" in results[0].output
        assert "survived" not in results[0].output
        assert "daemon-alive" in results[1].output
        assert any(isinstance(e, TurnComplete) for e in session.event_log.all_events)
        assert not task.done()
        listed = await send({"type": "list_sessions"})
        assert listed is not None
    finally:
        runner = daemon.session_registry.get_runner(sid) if "sid" in locals() else None
        if runner is not None:
            await runner.cancel()
        daemon._shutdown_event.set()
        await asyncio.wait_for(task, timeout=10.0)
