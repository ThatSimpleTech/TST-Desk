"""M9 exit harness (TD-4304).

Headless mock pass: fake rootless container, a signed two-step charter,
one Class A write on ``tst/auto/<slug>``, then a tripped breaker.
``main`` does not move. This harness is the M9 exit criterion. Not
``e2e_harness.run``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from .autonomy.charter import charter_slug, parse_charter
from .autonomy.checkpoint import auto_branch
from .autonomy.sandbox import RuntimeState, WhichFn
from .config import AutonomyConfig, EmbeddingsConfig, ModelConfig, Preset, TierConfig
from .daemon import Daemon
from .e2e_checks import HarnessResult, _check
from .mock import MockProvider, Script
from .protocol import AutonomySummary
from .session import Session

_STEERING = "# M9 harness\n\nKeep replies short.\n"
_OBJECTIVE = "Land the first M9 step"
_STEP1 = "step1.txt"
_BRAIN = "m9-brain"
_WORKER = "m9-worker"
_VALIDATOR = "m9-validator"
_BUDGET = 60.0
_WAIT = 45.0
_CLASS_A_HEADING = re.compile(r"^## .+ · Class A", re.MULTILINE)
_CHARTER = f"""\
---
objective: {_OBJECTIVE}
definition_of_done:
  - $ test -f {_STEP1}
  - $ test -f never-done.txt
source_of_truth: []
boundary:
  writable_paths:
    - "**"
  allowed_commands:
    - test
  network: deny
caps:
  spend_usd: 25.0
  wall_clock_hours: 8.0
  max_iterations: 20
stop_conditions:
  - no measurable progress for 2
---

Two-step DoD: the write turns the first item green; the second stays
red so the run iterates, then the no-progress breaker trips.
"""


def _tier(slug: str) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://127.0.0.1:9/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=8_000,
        max_output_tokens=1_000,
    )


def m9_config() -> ModelConfig:
    """Known slugs so the mock sequence is keyed without discovery."""
    return ModelConfig(
        presets={
            "m9-harness": Preset(
                brain=_tier(_BRAIN),
                worker=_tier(_WORKER),
                validator=_tier(_VALIDATOR),
            )
        },
        active_preset="m9-harness",
        embeddings=EmbeddingsConfig(base_url="", model=""),
        autonomy=AutonomyConfig(check_every=99),
    )


def _harness_config(*_args: object, **_kwargs: object) -> ModelConfig:
    return m9_config()


def auto_branch_name() -> str:
    """``tst/auto/<slug>`` for this harness charter."""
    return auto_branch(charter_slug(_OBJECTIVE))


async def _rootless(_binary: str) -> RuntimeState:
    return RuntimeState.ROOTLESS


def _which_for(path: Path) -> WhichFn:
    def _which(name: str) -> str | None:
        if name in {"podman", path.name}:
            return str(path)
        return None

    return _which


def _fake_runtime(tmp_path: Path) -> Path:
    """An executable that speaks enough of ``podman info`` (no real engine)."""
    script = tmp_path / "podman"
    script.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import sys",
                "if len(sys.argv) > 1 and sys.argv[1] == 'info':",
                "    print('true')",
                "    raise SystemExit(0)",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip()


def _prepare_workspace(workspace: Path, data_dir: Path) -> str:
    """Init a real repo, commit the charter on ``main``, return that SHA."""
    workspace.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(_STEERING, encoding="utf-8")
    charter_dir = workspace / ".tst" / "autonomy"
    charter_dir.mkdir(parents=True, exist_ok=True)
    (charter_dir / "CHARTER.md").write_text(_CHARTER, encoding="utf-8")
    parse_charter(_CHARTER)
    rc, _out = _git(workspace, "init", "-b", "main")
    if rc != 0:
        raise RuntimeError("git init failed for the M9 harness workspace")
    _git(workspace, "config", "user.name", "M9 Harness")
    _git(workspace, "config", "user.email", "m9@example.com")
    rc, _out = _git(workspace, "add", "-A")
    if rc != 0:
        raise RuntimeError("git add failed for the M9 harness workspace")
    rc, _out = _git(workspace, "commit", "-m", "harness baseline")
    if rc != 0:
        raise RuntimeError("git commit failed for the M9 harness workspace")
    rc, sha = _git(workspace, "rev-parse", "refs/heads/main")
    if rc != 0 or not sha:
        raise RuntimeError("harness workspace has no main SHA")
    return sha


def _provider() -> MockProvider:
    write = json.dumps({"path": _STEP1, "content": "ok\n"})
    return MockProvider(
        sequences={
            _BRAIN: [
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=write),
                Script(kind="stream", content="Wrote the first DoD step."),
                Script(kind="stream", content="Still working on the second step."),
            ]
        },
        scripts={
            _WORKER: Script(kind="text", content="RED"),
            _VALIDATOR: Script(
                kind="text",
                content=(
                    '{"serves_objective": true, "class_a_drifted": false, "progress_real": true}'
                ),
            ),
        },
        default=Script(kind="stream", content="Working"),
    )


async def _wait_done(session: Session, timeout_secs: float) -> str:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if session.state in {"complete", "failed", "cancelled"}:
            return session.state
        await asyncio.sleep(0.05)
    return session.state


def _stop_reason(session: Session) -> str:
    if session.autonomy_stop_reason:
        return session.autonomy_stop_reason
    for event in session.event_log.all_events:
        if isinstance(event, AutonomySummary) and event.reason:
            return event.reason
    return ""


def _class_a_line(workspace: Path) -> str:
    path = workspace / ".tst" / "autonomy" / "DECISIONS.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    match = _CLASS_A_HEADING.search(text)
    return match.group(0) if match else ""


async def _start(daemon: Daemon, workspace: Path) -> dict[str, Any]:
    raw = await daemon._handle_message(
        json.dumps({"type": "start_autonomy", "workspace_path": str(workspace)}),
        None,
    )
    if raw is None:
        return {}
    parsed: object = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


async def run_m9(workspace: Path, data_dir: Path) -> HarnessResult:
    """Drive the M9 exit: mock run, auto-branch commit, Class A, breaker."""
    started = time.monotonic()
    main_before = _prepare_workspace(workspace, data_dir)
    fake = _fake_runtime(data_dir)
    branch = auto_branch_name()
    start: dict[str, Any] = {}
    session: Session | None = None
    daemon: Daemon | None = None
    try:
        with (
            patch("tstd.daemon.cached_config", _harness_config),
            patch("tstd.autonomy.sandbox.shutil.which", _which_for(fake)),
            patch("tstd.autonomy.sandbox.inspect_runtime", _rootless),
        ):
            daemon = Daemon(data_dir=data_dir, provider=_provider())
            daemon.config = m9_config()
            start = await _start(daemon, workspace)
            session_id = start.get("session_id")
            if isinstance(session_id, str) and session_id:
                session = daemon.session_registry.get(session_id)
                if session is not None:
                    await _wait_done(session, _WAIT)
    finally:
        if daemon is not None:
            await daemon._shutdown()

    auto_ref = f"refs/heads/{branch}"
    rc_auto, auto_log = _git(workspace, "log", "--oneline", auto_ref)
    rc_ahead, ahead = _git(workspace, "rev-list", "--count", f"refs/heads/main..{auto_ref}")
    rc_main, main_after = _git(workspace, "rev-parse", "refs/heads/main")
    class_a = _class_a_line(workspace)
    reason = _stop_reason(session) if session is not None else ""
    ahead_n = int(ahead) if rc_ahead == 0 and ahead.isdigit() else 0
    result = HarnessResult(elapsed=time.monotonic() - started)
    result.notes.append("TD-4304 this harness is the M9 exit criterion")
    _check(
        result,
        "start ready",
        start.get("type") == "autonomy_start"
        and start.get("ready") is True
        and start.get("signed") is True
        and isinstance(start.get("session_id"), str),
        f"type={start.get('type')} error={start.get('error')!r}",
    )
    _check(
        result,
        "run completed",
        session is not None and session.state == "complete",
        f"state={None if session is None else session.state}",
    )
    _check(
        result,
        "auto branch commits",
        rc_auto == 0 and bool(auto_log) and ahead_n >= 1,
        f"branch={branch} ahead={ahead_n} log={auto_log[:80]!r}",
    )
    _check(
        result,
        "main unchanged",
        rc_main == 0 and main_after == main_before,
        f"before={main_before[:12]} after={main_after[:12]}",
    )
    _check(result, "class A ledger", bool(class_a), class_a or "no Class A heading")
    _check(
        result,
        "breaker tripped",
        reason.startswith("breaker:"),
        reason or "no stop reason",
    )
    _check(result, "under budget", result.elapsed < _BUDGET, f"{result.elapsed:.1f}s")
    _check(result, "m9 exit criterion", result.ok, "mock run + branch + Class A + breaker")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M9 exit harness — mock autonomy pass (TD-4304)")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    scratch = tempfile.TemporaryDirectory(prefix="tstd-e2e-m9-")
    root = Path(scratch.name)
    workspace = args.workspace or root / "workspace"
    data_dir = args.data_dir or root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_m9(workspace, data_dir))
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
