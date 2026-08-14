"""Performance baselines (TD-1404).

Measures the four core metrics the story names:

- ``daemon_cold_start`` — Daemon() to a connectable WebSocket
  (``port.json`` written), the harness's readiness signal.
- ``session_start_large_repo`` — ``open_workspace`` to the first
  ``session_state`` over the real protocol, against a synthesized
  2,000-file git repository.
- ``steering_resolution`` — ``ContextAssembler.assemble_sync`` over a
  workspace with AGENTS.md plus twelve path-scoped rule files.
- ``first_token_latency`` — ``user_message`` to the first
  ``assistant_delta`` with an instant mock provider.  This measures the
  internal pipeline (assembly, routing, event fan-out), not network or
  model time — the baseline metadata says so.

The fifth metric, ``timeline_render_1000``, is measured by the UI bench
(ui/src/lib/timeline-bench.test.ts) — pytest cannot mount a Svelte
component — and gated there against the same perf_baselines.json; it is
listed in ``UI_MEASURED`` so a missing baseline row fails loudly here too.

``tests/test_benchmarks.py`` compares a fresh measurement against the
committed ``tests/perf_baselines.json`` and fails beyond the stated
threshold; ``scripts/benchmarks.py --record`` re-baselines deliberately.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from websockets.asyncio.client import connect

from .config import cached_config
from .context.assembler import ContextAssembler
from .daemon import Daemon
from .loop import agent_loop
from .mock import MockProvider, Script
from .router import TierRouter
from .session import Session, SessionRunner

# ── Workload synthesis ──────────────────────────────────────────────────


def _prepare_dir(path: Path) -> None:
    """Sync mkdir: ASYNC240 keeps pathlib calls out of async functions."""
    path.mkdir(parents=True, exist_ok=True)


def _prepare_token_workspace(workspace: Path) -> None:
    """Sync workspace setup for the first-token measurement (ASYNC240)."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text("# Bench\n", encoding="utf-8")


def synthesize_large_repo(root: Path, *, dirs: int = 100, files_per_dir: int = 20) -> None:
    """A 2,000-file git repository: the "large repo" session-start surface."""
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    for d in range(dirs):
        directory = root / f"pkg{d:03d}"
        directory.mkdir()
        for f in range(files_per_dir):
            (directory / f"mod{f:02d}.py").write_text(
                f'"""Module {f} of package {d}."""\n\nVALUE = {d * 100 + f}\n',
                encoding="utf-8",
            )
    (root / "AGENTS.md").write_text(
        "# Benchmark workspace\n\nKeep changes small.\n", encoding="utf-8"
    )
    for args in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Bench", "-c", "user.email=bench@localhost", "commit", "-qm", "baseline"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def synthesize_steering_workspace(root: Path, *, rules: int = 12) -> None:
    """AGENTS.md plus N frontmatter-carrying rule files (TD-503 shape)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(
        "# Benchmark workspace\n\n" + "Steering body text. " * 200 + "\n", encoding="utf-8"
    )
    rules_dir = root / ".tst" / "rules"
    rules_dir.mkdir(parents=True)
    for i in range(rules):
        (rules_dir / f"rule{i:02d}.md").write_text(
            f'---\nappliesTo: ["pkg{i:03d}/**"]\n---\n\n# Rule {i}\n\n'
            + "Follow the house style. " * 60
            + "\n",
            encoding="utf-8",
        )


# ── Measurements ────────────────────────────────────────────────────────
# Each returns seconds; callers take medians across repetitions.


async def measure_daemon_cold_start(data_dir: Path) -> float:
    """Daemon() → port.json connectable.  Daemon is shut down after."""
    await asyncio.to_thread(_prepare_dir, data_dir)
    started = time.monotonic()
    daemon = Daemon(
        data_dir=data_dir, provider=MockProvider(default=Script(kind="stream", content=""))
    )
    task = asyncio.create_task(daemon.run())
    try:
        deadline = started + 15.0
        while not (data_dir / "port.json").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("daemon did not write port.json")
            await asyncio.sleep(0.01)
        return time.monotonic() - started
    finally:
        daemon._shutdown_event.set()
        await asyncio.wait_for(task, timeout=5.0)


async def measure_session_start_large_repo(repo: Path, data_dir: Path) -> float:
    """Warm daemon: open_workspace → first session_state on a large repo."""
    await asyncio.to_thread(_prepare_dir, data_dir)
    daemon = Daemon(
        data_dir=data_dir, provider=MockProvider(default=Script(kind="stream", content=""))
    )
    task = asyncio.create_task(daemon.run())
    try:
        deadline = time.monotonic() + 15.0
        while not (data_dir / "port.json").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("daemon did not write port.json")
            await asyncio.sleep(0.01)
        info = json.loads((data_dir / "port.json").read_text(encoding="utf-8"))
        async with connect(f"ws://127.0.0.1:{info['port']}") as ws:
            await ws.send(json.dumps({"type": "hello", "token": info["token"], "version": 1}))
            await ws.recv()  # hello_ack
            started = time.monotonic()
            await ws.send(json.dumps({"type": "open_workspace", "path": str(repo)}))
            state = json.loads(await asyncio.wait_for(ws.recv(), timeout=30.0))
            elapsed = time.monotonic() - started
            if state.get("type") != "session_state":
                raise AssertionError(f"expected session_state, got {state.get('type')!r}")
            await ws.send(json.dumps({"type": "shutdown"}))
            return elapsed
        raise AssertionError("unreachable")  # pragma: no cover
    finally:
        if not task.done():
            daemon._shutdown_event.set()
            await asyncio.wait_for(task, timeout=5.0)


def measure_steering_resolution(workspace: Path) -> float:
    """One full steering assembly over the synthesized workspace."""
    started = time.monotonic()
    ContextAssembler().assemble_sync(workspace)
    return time.monotonic() - started


async def measure_first_token_latency(workspace: Path) -> float:
    """user_message → first assistant_delta, mock provider (pipeline only)."""
    await asyncio.to_thread(_prepare_token_workspace, workspace)
    brain = cached_config().tier("brain").slug
    mock = MockProvider(
        sequences={brain: [Script(kind="stream", content="hello from the mock")]},
        default=Script(kind="stream", content="(unused)"),
    )
    session = Session(str(workspace))

    async def factory() -> MockProvider:
        return mock

    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(s, TierRouter(), factory, cached_config()),
    )
    await runner.start()
    try:
        seen = 0
        started = time.monotonic()
        await session.add_user_message("say hello")
        deadline = started + 15.0
        while time.monotonic() < deadline:
            new_seq = await asyncio.wait_for(
                session.event_log.wait_for_new_event(seen), timeout=15.0
            )
            for event in session.event_log.events_from(seen + 1):
                if event.type == "assistant_delta":
                    return time.monotonic() - started
            seen = new_seq
        raise TimeoutError("no assistant_delta within 15s")
    finally:
        await session.cancel()


# ── Driver ──────────────────────────────────────────────────────────────


async def _median(repetitions: int, measure: Callable[[], Awaitable[float]]) -> float:
    samples = [await measure() for _ in range(repetitions)]
    return statistics.median(samples)


def _fresh(scratch: Path, label: str) -> Path:
    path = scratch / label
    path.mkdir(parents=True, exist_ok=True)
    return path


async def daemon_cold_start(scratch: Path) -> float:
    """Median of 3 cold starts, each against a fresh data dir."""
    counter = 0

    async def _once() -> float:
        nonlocal counter
        counter += 1
        return await measure_daemon_cold_start(_fresh(scratch, f"cold{counter}"))

    return await _median(3, _once)


async def session_start_large_repo(scratch: Path) -> float:
    """Median of 3 opens against one synthesized 2,000-file repo."""
    repo = scratch / "large-repo"
    await asyncio.to_thread(synthesize_large_repo, repo)
    counter = 0

    async def _once() -> float:
        nonlocal counter
        counter += 1
        return await measure_session_start_large_repo(repo, _fresh(scratch, f"sess{counter}"))

    return await _median(3, _once)


async def steering_resolution(scratch: Path) -> float:
    """Median of 5 assemblies over the synthesized steering workspace."""
    workspace = scratch / "steering-ws"
    await asyncio.to_thread(synthesize_steering_workspace, workspace)
    return await _median(5, lambda: asyncio.to_thread(measure_steering_resolution, workspace))


async def first_token_latency(scratch: Path) -> float:
    """Median of 5 message → first-delta round trips (pipeline only)."""
    counter = 0

    async def _once() -> float:
        nonlocal counter
        counter += 1
        return await measure_first_token_latency(_fresh(scratch, f"token{counter}"))

    return await _median(5, _once)


# Measured metrics, in report order.  The regression test parametrizes
# over this mapping, so a new metric is gated the moment it is added.
MEASURERS: dict[str, Callable[[Path], Awaitable[float]]] = {
    "daemon_cold_start": daemon_cold_start,
    "session_start_large_repo": session_start_large_repo,
    "steering_resolution": steering_resolution,
    "first_token_latency": first_token_latency,
}

# Named-but-unmeasurable metrics, with the story that unblocks them.
PENDING: dict[str, str] = {}

# Metrics pytest cannot measure (they need the Svelte component tree), gated
# by the UI bench against the same perf_baselines.json instead (TD-1404).
UI_MEASURED: dict[str, str] = {
    "timeline_render_1000": "ui/src/lib/timeline-bench.test.ts",
}


@dataclass
class BenchReport:
    """Metric id → median seconds (None while a metric is pending)."""

    metrics: dict[str, float | None] = field(default_factory=dict)

    def render(self) -> str:
        lines = ["metric                      median"]
        for name, value in self.metrics.items():
            rendered = f"{value * 1000:8.0f} ms" if value is not None else "  pending"
            lines.append(f"{name:<28}{rendered}")
        return "\n".join(lines)


async def run_all(scratch: Path) -> BenchReport:
    """Measure every core metric; each repetition gets a fresh scratch dir.

    UI-measured metrics (``UI_MEASURED``) are left untouched — the UI bench
    owns their values in the shared baselines file.
    """
    report = BenchReport()
    for name, measurer in MEASURERS.items():
        report.metrics[name] = await measurer(scratch / name)
    return report
