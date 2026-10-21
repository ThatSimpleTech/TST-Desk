"""Memory cap on shell commands (TD-5116).

Acceptance criteria:

1. ``caps.shell_memory_mb`` (default about half of RAM) applies through a
   cgroup scope where systemd user scopes exist, otherwise through
   ``RLIMIT_AS``. Podman runs get ``--memory``.
2. A command over the cap is killed. The tool result says so plainly.
3. A memory hog is killed and the daemon survives.

The scope tests need a systemd user manager with the memory controller
(TOWER has one) and skip without it; the ``RLIMIT_AS`` tests force that
path, so CI runs them either way. Every hog stops on its own at 2 GiB, so
a cap that fails to apply fails the test instead of the machine.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_dispatch import make_config
from tests.test_sandbox import _fake_runtime, _which_for
from tstd.autonomy.charter import Charter
from tstd.autonomy.charter_io import write_charter_document
from tstd.autonomy.sandbox import autonomy_shell_argv, sandbox_exec
from tstd.boundary_config import BoundaryConfig, boundary_from_mapping
from tstd.config import ConfigError
from tstd.daemon import Daemon
from tstd.host_memory import MIB, default_shell_memory_mb, physical_memory_mb
from tstd.mock import MockProvider, Script
from tstd.protocol import ApprovalRequest, ToolResult, TurnComplete
from tstd.session import Session
from tstd.session_lifecycle import reload_session_boundary
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
        # TD-5160: no swap on top, as the host scope's MemorySwapMax=0.
        assert "--memory-swap=96m" in argv
        assert argv.index("--memory-swap=96m") < argv.index("alpine")

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
    @pytest.mark.parametrize(
        ("in_file", "in_charter", "expect"),
        [(96, None, 96), (96, 128, 128), (None, 128, 128)],
        ids=["file-when-charter-leaves-it", "charter-wins", "charter-alone"],
    )
    async def test_an_autonomous_command_runs_under_the_run_cap(
        self, tmp_path: Path, in_file: int | None, in_charter: int | None, expect: int
    ) -> None:
        """Through the charter flow a run really takes: write, sign, reload."""
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
        (ws / ".tst").mkdir(parents=True)
        if in_file is not None:
            (ws / ".tst" / "config.yaml").write_text(
                yaml.safe_dump({"caps": {"shell_memory_mb": in_file}}), encoding="utf-8"
            )
        caps: dict[str, Any] = {"spend_usd": 5, "wall_clock_hours": 1, "max_iterations": 10}
        if in_charter is not None:
            caps["shell_memory_mb"] = in_charter
        charter, _ = write_charter_document(
            ws,
            {
                "objective": "build",
                "definition_of_done": ["tests pass"],
                "boundary": {"writable_paths": ["**"], "network": "deny"},
                "caps": caps,
            },
        )
        session = Session(str(ws))
        session.autonomy = True
        session.charter = charter
        session.config = make_config()
        session.config.autonomy.runtime = str(runtime)
        assert reload_session_boundary(session, str(ws)) is None
        # The charter's other caps still replace the file's.
        assert session.boundary_config.caps.max_iterations == 10
        result = await run_shell(session, "true")
        assert f"--memory={expect}m" in json.loads(log.read_text(encoding="utf-8"))
        assert "exit code: 137" in result
        assert f"memory cap: killed (exit 137) in a container capped at {expect} MB" in result

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
            # TD-5160: what RLIMIT_AS makes dlopen and a JVM's threads say.
            "error while loading shared libraries: libz.so.1: failed to map segment "
            "from shared object",
            "java.lang.OutOfMemoryError: unable to create native thread: possibly out of memory",
            "java.lang.OutOfMemoryError: unable to create new native thread",
        ],
    )
    def test_the_usual_runtimes_are_read(self, stderr: str) -> None:
        assert ran_out_of_memory(stderr)

    @pytest.mark.parametrize(
        "stderr",
        [
            "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory",
            # TD-5160: GPU memory, which the cap does not set.
            "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
            "RuntimeError: CUDA error: out of memory",
            "torch.OutOfMemoryError: HIP out of memory. Tried to allocate 20.00 MiB",
            "RuntimeError: MPS backend out of memory (MPS allocated: 1 GB)",
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
        # Read from the scope's own counters it is certain; once systemd has
        # collected the scope, Result=oom-kill also counts the system's
        # killers, so it is the likely cause (TD-5160).
        assert (
            "memory cap: killed — the command went over its 64 MB memory cap (caps.shell_memory_mb)"
            in result
            or "under its 64 MB memory cap (caps.shell_memory_mb); going over that cap "
            "is the usual cause"
            in result
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

    async def test_the_scope_never_takes_the_command_as_its_description(
        self, tmp_path: Path
    ) -> None:
        # The scope reads its own Description: the unit is the last part of
        # its cgroup path. A fake token, new each run so an older journal
        # line cannot match, rides along in a comment.
        secret = f"ghp_{uuid.uuid4().hex}0000"
        command = (
            "u=$(sed -n 's|^0::.*/||p' /proc/self/cgroup); "
            f'systemctl --user show --property=Description --value "$u" # {secret}'
        )
        result = await run_shell(_session(tmp_path, 64), command)
        stdout = result.split("── stdout ──")[1].split("── stderr ──")[0].strip()
        assert stdout == "tstd shell command"
        journal = await asyncio.to_thread(
            subprocess.run,
            ["journalctl", "--user", "--since=-2min", "-o", "cat", "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert secret not in journal.stdout

    async def test_the_command_text_reaches_the_shell_unexpanded(self, tmp_path: Path) -> None:
        # systemd 254+ rewrites ``$$`` and ``${x}`` in systemd-run arguments
        # unless told not to.
        command = 'x=kept; echo "[${x}]"; [ "$$" -gt 1 ] && echo pid-ok; printf \'%s|\' "$0"'
        result = await run_shell(_session(tmp_path, 64), command)
        assert "[kept]" in result
        assert "pid-ok" in result
        assert "/bin/sh|" in result


# ── The scope's journal line never carries the command ─────────────────


_SECRET = "ghp_" + "x" * 36


def test_the_scope_description_is_fixed_text_not_the_command() -> None:
    """systemd logs a scope's Description in its "Started" journal line.

    Its default is the command line, so without ``--description`` every
    command, secrets and all, would go to the journal unredacted.
    """
    tools = shell_memory._ScopeTools("systemd-run", "systemctl", no_expand=True)
    cap = ShellMemoryCap(mb=64, method="scope", unit="u.scope", tools=tools)
    command = f"curl -H 'Authorization: Bearer {_SECRET}' https://example.invalid"
    argv = shlex.split(shell_memory.host_command(cap, command))
    split = argv.index("--")
    assert "--description=tstd shell command" in argv[:split]
    # The command is only ever the scope's payload, never one of its settings.
    assert not any(_SECRET in arg for arg in argv[:split])
    assert argv[split + 1 :] == ["/bin/sh", "-c", command]


# ── Review notes (TD-5160): whose kill it was, and a scope that never started


class TestScopeVerdict:
    @staticmethod
    def _cap() -> ShellMemoryCap:
        tools = shell_memory._ScopeTools("systemd-run", "systemctl", False, True)
        return ShellMemoryCap(mb=64, method="scope", unit="u.scope", tools=tools)

    @staticmethod
    def _state(
        monkeypatch: pytest.MonkeyPatch, state: dict[str, str], events: tuple[int, int] | None
    ) -> None:
        async def _unit(_cap: ShellMemoryCap, _tools: Any) -> dict[str, str]:
            return state

        async def _quiet(*_args: Any) -> str:
            return ""

        monkeypatch.setattr(shell_memory, "_unit_state", _unit)
        monkeypatch.setattr(shell_memory, "_systemctl", _quiet)
        monkeypatch.setattr(shell_memory, "_cgroup_oom_events", lambda _cg: events)

    async def test_the_scope_reaching_its_cap_is_certain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._state(monkeypatch, {"ControlGroup": "/u.scope", "ActiveState": "failed"}, (1, 1))
        note = await cap_note(self._cap(), 137, "Killed")
        assert note.startswith("memory cap: killed — the command went over its 64 MB")

    async def test_a_kill_without_the_cap_reached_is_not_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The system-wide killer or systemd-oomd took it: oom_kill, no oom."""
        state = {"ControlGroup": "/u.scope", "ActiveState": "failed", "Result": "oom-kill"}
        self._state(monkeypatch, state, (0, 1))
        assert await cap_note(self._cap(), 137, "Killed") == ""
        # Nor on systemd before 253, where a bare SIGKILL is otherwise the cue.
        before_253 = shell_memory._ScopeTools("systemd-run", "systemctl", False, False)
        old_cap = ShellMemoryCap(mb=64, method="scope", unit="u.scope", tools=before_253)
        assert await cap_note(old_cap, 137, "Killed") == ""

    async def test_only_systemds_result_left_is_the_likely_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._state(monkeypatch, {"Result": "oom-kill", "ActiveState": "failed"}, None)
        note = await cap_note(self._cap(), 137, "Killed")
        assert note.startswith("memory cap: killed (exit 137) under its 64 MB memory cap")
        assert "usual cause" in note

    @pytest.mark.parametrize(
        "stderr",
        [
            "Failed to connect to bus: No medium found",
            "Failed to start transient scope unit: Connection timed out",
            "Failed to create bus connection: No such file or directory",
        ],
    )
    async def test_a_scope_that_never_started_says_so_and_probes_again(
        self, monkeypatch: pytest.MonkeyPatch, stderr: str
    ) -> None:
        """The user bus went away after the probe: the command did not run."""
        self._state(monkeypatch, {}, None)
        shell_memory._probe.done = True
        shell_memory._probe.tools = self._cap().tools
        note = await cap_note(self._cap(), 1, stderr + "\n")
        assert note.startswith("memory cap: systemd-run reported that it could not start")
        assert "did not run" in note
        assert shell_memory._probe.done is False

    @pytest.mark.parametrize(
        ("exit_code", "stderr"),
        [
            (0, "Failed to connect to bus: x"),
            (3, "Failed to connect to bus: No medium found"),
            (1, "building...\nFailed to connect to bus: No medium found"),
        ],
    )
    async def test_a_command_that_prints_such_a_line_itself_is_not_mistaken(
        self, monkeypatch: pytest.MonkeyPatch, exit_code: int, stderr: str
    ) -> None:
        """A command that ran systemctl can print the line too; only systemd-run's
        lone line and exit 1 count."""
        self._state(monkeypatch, {}, None)
        shell_memory._probe.done = True
        assert await cap_note(self._cap(), exit_code, stderr) == ""
        assert shell_memory._probe.done is True


class TestProbeRetries:
    async def test_a_probe_that_found_nothing_runs_again_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One timeout at startup must not pin the daemon to RLIMIT_AS."""
        answers: list[shell_memory._ScopeTools | None] = [
            None,
            shell_memory._ScopeTools("systemd-run", "systemctl", True),
        ]

        async def _probe(_env: dict[str, str]) -> shell_memory._ScopeTools | None:
            return answers.pop(0)

        now = [1_000.0]
        monkeypatch.setattr(shell_memory, "_probe_scope_tools", _probe)
        monkeypatch.setattr(shell_memory, "_is_linux", lambda: True)
        monkeypatch.setattr(shell_memory.time, "monotonic", lambda: now[0])
        session = _session(Path("."), 256)
        first = await plan_cap(session, {})
        assert first is not None and first.method == "rlimit"
        assert (await plan_cap(session, {})).method == "rlimit"  # type: ignore[union-attr]
        now[0] += shell_memory._REPROBE_SECS
        again = await plan_cap(session, {})
        assert again is not None and again.method == "scope"
        assert answers == []


# ── systemd before 253: no OOMPolicy, no oom-kill result ────────────────


class TestScopeWithoutOomPolicy:
    @pytest.fixture(autouse=True)
    def _collected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """systemd has collected the scope: no Result, no control group."""

        async def _gone(_cap: ShellMemoryCap, _tools: Any) -> dict[str, str]:
            return {}

        monkeypatch.setattr(shell_memory, "_unit_state", _gone)

    @staticmethod
    def _cap(*, oom_policy: bool) -> ShellMemoryCap:
        tools = shell_memory._ScopeTools("systemd-run", "systemctl", False, oom_policy)
        return ShellMemoryCap(mb=64, method="scope", unit="u.scope", tools=tools)

    @pytest.mark.parametrize("exit_code", [137, -9])
    async def test_a_sigkill_is_put_down_to_the_cap(self, exit_code: int) -> None:
        note = await cap_note(self._cap(oom_policy=False), exit_code, "")
        assert note.startswith(f"memory cap: killed (exit {exit_code}) under its 64 MB memory cap")
        assert "usual cause" in note

    @pytest.mark.parametrize("exit_code", [0, 1, 143])
    async def test_any_other_exit_gets_no_note(self, exit_code: int) -> None:
        assert await cap_note(self._cap(oom_policy=False), exit_code, "") == ""

    async def test_systemd_253_and_later_still_needs_its_own_word(self) -> None:
        # There systemd records Result=oom-kill, so a bare SIGKILL is not the cap.
        assert await cap_note(self._cap(oom_policy=True), 137, "") == ""


def _fake_systemd(tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A ``systemd-run`` that answers the probe the way *mode* says.

    ``old`` refuses ``--expand-environment`` and passes text through, as
    systemd 253 does; ``pre253`` also refuses ``OOMPolicy`` on a scope, as
    systemd 246-252 does; ``expands`` takes the flag but loses the marker
    anyway; ``works`` passes. Each call's argv is appended to
    ``calls.jsonl``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemd-run").write_text(
        f"#!{sys.executable}\nimport json, sys\nmode = {mode!r}\n"
        f"open({str(tmp_path / 'calls.jsonl')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if mode in ('old', 'pre253') and '--expand-environment=no' in sys.argv:\n"
        "    sys.exit('systemd-run: unrecognized option')\n"
        "if mode == 'pre253' and '--property=OOMPolicy=stop' in sys.argv:\n"
        "    sys.exit('Failed to start transient scope unit: Cannot set property OOMPolicy, "
        "or unknown property.')\n"
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

    async def test_systemd_before_253_still_gets_a_scope_without_oom_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Ubuntu 22.04 (249), Debian 12 and RHEL 9 (252): scopes take
        # MemoryMax but refuse OOMPolicy, which the cap does not need.
        tools = await shell_memory._probe_scope_tools(
            _fake_systemd(tmp_path, "pre253", monkeypatch)
        )
        assert tools is not None
        assert (tools.no_expand, tools.oom_policy) == (False, False)
        cap = ShellMemoryCap(mb=64, method="scope", unit="u.scope", tools=tools)
        argv = shlex.split(shell_memory.host_command(cap, "true"))
        assert "--property=MemoryMax=64M" in argv
        assert "--property=MemorySwapMax=0" in argv
        assert not any("OOMPolicy" in arg for arg in argv)

    async def test_systemd_253_keeps_oom_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tools = await shell_memory._probe_scope_tools(_fake_systemd(tmp_path, "old", monkeypatch))
        assert tools is not None and tools.oom_policy

    async def test_the_probe_never_logs_its_command_as_the_description(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await shell_memory._probe_scope_tools(_fake_systemd(tmp_path, "works", monkeypatch))
        (argv,) = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert "--description=tstd shell memory probe" in argv
        assert argv.index("--description=tstd shell memory probe") < argv.index("--")

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

    ``best`` is whatever the machine has (a scope on TOWER, ``RLIMIT_AS`` on
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
