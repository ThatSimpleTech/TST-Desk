"""Measure stale-result clearing against a live model (TD-5162, for TD-5115).

Runs one long, tool-heavy session through a separate ``tstd`` with its own
scratch ``--data-dir`` (never the window's), on a scratch git repo seeded
with real ``tstd`` modules. Every turn is a ``tst run --continue`` (TD-5111)
that reads and greps files. Run it twice, ``--clearing on`` and
``--clearing off``, with the same seed and turns, then compare:

    uv run python scripts/measure_clear_stale.py run --clearing off \\
        --root /tmp/td5162/off --config ~/.local/share/tst-desk/config.yaml
    uv run python scripts/measure_clear_stale.py summarize /tmp/td5162/off

``run`` copies the given config.yaml (and ``token-calibration.json`` beside
it, when present) into the scratch data dir, sets only
``clear_tool_results.enabled`` and ``session.log_max_events``, and before
each turn waits for the model server's ``/slots`` to be idle, so it never
runs while someone else's request is in flight. It stops early at
``--max-minutes``. The daemon it started is stopped at the end.

``summarize`` counts real compactions (``context_compacted`` with the
trigger ``auto``) apart from in-turn trims (``inflight_trim``) and clears
(``clear_stale``), and reads each turn's peak real prompt tokens from the
scratch ``audit.db``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

CORE = Path(__file__).resolve().parent.parent

#: Real ``tstd`` modules of 7k to 13k characters, copied under ``src/``.
SEED_FILES = (
    "router.py", "fallback.py", "hub.py", "peer_policy.py", "bridge_machine.py",
    "bridge_wire.py", "bridge_lane.py", "machines.py", "machine_pairing.py",
    "tailscale_bind.py", "audit_queries.py", "questions.py", "discovery.py",
    "shutdown_phases.py", "memory_distill.py", "plan_review.py", "logging.py",
    "attachments.py", "git_restore.py", "provider_refresh.py", "stop_check.py",
    "proc_lifecycle.py", "autonomy/dod.py", "autonomy/ledger.py",
    "autonomy/judgment.py", "autonomy/breakers.py", "autonomy/revert.py",
    "autonomy/sandbox.py", "context/embeddings.py", "context/skills.py",
    "context/manifest.py", "context/tier.py", "context/memory_loader.py",
    "context/imports.py", "context/discover.py", "tools/web_extract.py",
    "tools/web_page.py", "tools/boundary.py", "tools/plugins.py", "tools/consult.py",
)  # fmt: skip

#: Every sixth turn asks about a file read turns ago, without asking to read it.
RECALL_EVERY = 6


def build_prompts(files: tuple[str, ...] = SEED_FILES) -> list[str]:
    """The session's messages: pairs of reads plus a grep, and recall turns."""
    prompts: list[str] = []
    pairs = [(files[i], files[i + 1]) for i in range(0, len(files) - 1, 2)]
    for n, (a, b) in enumerate(pairs, start=1):
        module = Path(a).stem
        prompts.append(
            f"Read src/{a} and src/{b} in full with fs_read (no line limits), then grep "
            f"src/ for `import {module}|{module} import`. In at most four sentences: what "
            "does each file do, and does any other file use the first one?"
        )
        if n % (RECALL_EVERY - 1) == 0:
            earlier = pairs[max(0, n - 4)][0]
            prompts.append(
                f"Back to src/{earlier}, which you read earlier in this session: name two "
                "functions it defines and the file you have read since that is most related "
                "to it. Three sentences at most."
            )
    return prompts


def seed_repo(dest: Path, source: Path = CORE / "tstd") -> None:
    """A git repo holding ``SEED_FILES`` under ``src/``, committed once."""
    for rel in SEED_FILES:
        target = dest / "src" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
    git = ["git", "-C", str(dest), "-c", "user.name=measure", "-c", "user.email=measure@local"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "seed"], check=True)


def scratch_config(config: dict[str, Any], clearing: bool) -> dict[str, Any]:
    """*config* with only the clearing switch and the event-log size changed."""
    out = dict(config)
    out["clear_tool_results"] = {
        **(config.get("clear_tool_results") or {}),
        "enabled": clearing,
    }
    # Deltas are events too; keep the whole session so no compaction rotates out.
    out["session"] = {**(config.get("session") or {}), "log_max_events": 1_000_000}
    return out


def prepare_data_dir(data_dir: Path, config_path: Path, clearing: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    text = yaml.safe_dump(scratch_config(config, clearing), sort_keys=False)
    (data_dir / "config.yaml").write_text(text, encoding="utf-8")
    calibration = config_path.with_name("token-calibration.json")
    if calibration.exists():
        shutil.copyfile(calibration, data_dir / calibration.name)


def slots_busy(slots_url: str) -> bool:
    with urllib.request.urlopen(slots_url, timeout=10) as response:
        slots = json.loads(response.read())
    return any(slot.get("is_processing") for slot in slots)


def wait_for_idle(slots_url: str, max_wait: float) -> bool:
    deadline = time.monotonic() + max_wait
    while slots_busy(slots_url):
        if time.monotonic() > deadline:
            return False
        time.sleep(10)
    return True


def stop_daemon(data_dir: Path) -> None:
    """Stop the scratch daemon, and only if its command line names *data_dir*."""
    port_file = data_dir / "port.json"
    if not port_file.exists():
        return
    pid = int(json.loads(port_file.read_text(encoding="utf-8")).get("pid", 0))
    if pid > 0 and runs_with(pid, data_dir):
        os.kill(pid, signal.SIGTERM)


def runs_with(pid: int, data_dir: Path) -> bool:
    """True when process *pid* has *data_dir* as one whole argument.

    A whole argument, not a substring: /tmp/x/data must not match the
    daemon of /tmp/x/data2 (TD-5161). Linux reads /proc. macOS has no
    /proc (TD-5187), so ``ps`` prints the arguments joined by spaces and the
    match is on a whole space-delimited word.
    """
    wanted = (str(data_dir), f"--data-dir={data_dir}")
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists():
        args = cmdline.read_text(errors="replace").split("\0")
        return any(arg in args for arg in wanted)
    try:
        shown = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return False
    padded = f" {shown.strip()} "
    return any(f" {arg} " in padded for arg in wanted)


def run(args: argparse.Namespace) -> int:
    root: Path = args.root.resolve()
    data_dir, workspace, turns_dir = root / "data", root / "repo", root / "turns"
    if root.exists() and any(root.iterdir()):
        print(f"{root} is not empty; pick a fresh --root", file=sys.stderr)
        return 2
    seed_repo(workspace)
    prepare_data_dir(data_dir, args.config.expanduser(), args.clearing == "on")
    turns_dir.mkdir(parents=True)
    prompts = build_prompts()[: args.turns]
    started = time.monotonic()
    log = (root / "turns.jsonl").open("a", encoding="utf-8")
    try:
        for i, message in enumerate(prompts, start=1):
            if (time.monotonic() - started) / 60 > args.max_minutes:
                print(f"stopping before turn {i}: past {args.max_minutes} minutes")
                break
            if not wait_for_idle(args.slots_url, args.max_idle_wait):
                print(f"stopping before turn {i}: the model server stayed busy")
                break
            cmd = [str(Path(sys.executable).with_name("tst")), "run", "--data-dir", str(data_dir)]
            cmd += ["--workspace", str(workspace), "--message", message, "--output", "ndjson"]
            cmd += ["--on-ask", "deny-and-continue", "--timeout", str(args.turn_timeout)]
            if i > 1:
                cmd.append("--continue")
            t0 = time.monotonic()
            with (turns_dir / f"turn-{i:02d}.ndjson").open("w", encoding="utf-8") as out:
                code = subprocess.run(cmd, stdout=out, cwd=CORE, check=False).returncode
            row = {"turn": i, "exit": code, "seconds": round(time.monotonic() - t0, 1)}
            log.write(json.dumps(row) + "\n")
            log.flush()
            print(json.dumps(row), flush=True)
    finally:
        log.close()
        stop_daemon(data_dir)
    print(f"minutes: {(time.monotonic() - started) / 60:.1f}")
    return 0


def read_turn_events(turns_dir: Path) -> list[list[dict[str, Any]]]:
    """Each turn's events from ``turn-NN.ndjson``, a seq seen before dropped."""
    seen: set[tuple[str, int]] = set()
    turns: list[list[dict[str, Any]]] = []
    for path in sorted(turns_dir.glob("turn-*.ndjson")):
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            key = (str(event.get("session_id")), int(event.get("seq", -1)))
            if key in seen and key[1] >= 0:
                continue
            seen.add(key)
            events.append(event)
        turns.append(events)
    return turns


def peak_prompt_tokens(audit_db: Path) -> list[int | None]:
    """Each completed turn's largest real prompt (provider usage), in order.

    The audit writes a turn row only at ``turn_complete``, so these pair
    with the turns that completed, in the order they did; a turn with no
    model call of its own still has a row (None), so later ones stay paired.
    """
    if not audit_db.exists():
        return []
    with sqlite3.connect(audit_db) as db:
        rows = db.execute(
            "SELECT t.id, MAX(m.prompt_tokens) FROM turns t"
            " LEFT JOIN model_calls m ON m.turn_id = t.id AND m.is_classifier = 0"
            " GROUP BY t.id ORDER BY t.id"
        ).fetchall()
    return [None if peak is None else int(peak) for _, peak in rows]


def summarize_turns(turns: list[list[dict[str, Any]]], peaks: list[int | None]) -> dict[str, Any]:
    """Counts for the whole run, and one row per turn."""
    triggers: Counter[str] = Counter()
    cleared = 0
    read_before: set[str] = set()
    rereads = 0
    rows = []
    sessions = set()
    completed = 0
    for n, events in enumerate(turns, start=1):
        # Failed until a clean turn_complete says otherwise: a turn cut off
        # by the timeout never sends one.
        row: dict[str, Any] = {
            "turn": n,
            "auto": 0,
            "clear": 0,
            "trim": 0,
            "reads": 0,
            "failed": True,
        }
        peak: int | None = None
        estimates = [0]
        for event in events:
            kind = event.get("type")
            if event.get("session_id"):
                sessions.add(event["session_id"])
            if kind == "context_compacted":
                trigger = str(event.get("trigger", "auto"))
                triggers[trigger] += 1
                key = {"auto": "auto", "clear_stale": "clear", "inflight_trim": "trim"}
                if trigger in key:
                    row[key[trigger]] += 1
                if trigger == "clear_stale":
                    cleared += int(event.get("dropped_messages", 0))
            elif kind == "context_usage":
                estimates.append(int(event.get("used_tokens", 0)))
                row["compact_at"] = int(event.get("compact_at", 0))
            elif kind == "tool_call" and event.get("name") == "fs_read":
                path = str((event.get("arguments") or {}).get("path", ""))
                row["reads"] += 1
                rereads += path in read_before
                read_before.add(path)
            elif kind == "turn_complete":
                row["failed"] = bool(event.get("failed")) or bool(event.get("error_code"))
                row["seconds"] = round(float(event.get("duration", 0.0)), 1)
                if completed < len(peaks):
                    peak = peaks[completed]
                completed += 1
        row["peak_estimate"] = max(estimates)
        row["peak_prompt"] = peak
        rows.append(row)
    real = [r["peak_prompt"] for r in rows if r["peak_prompt"]]
    return {
        "turns": len(turns),
        "sessions": len(sessions),
        "failed_turns": sum(1 for r in rows if r.get("failed")),
        "compactions": triggers.get("auto", 0),
        "clears": triggers.get("clear_stale", 0),
        "results_cleared": cleared,
        "inflight_trims": triggers.get("inflight_trim", 0),
        "fs_reads": sum(r["reads"] for r in rows),
        "rereads": rereads,
        "peak_prompt_max": max(real, default=0),
        "peak_prompt_mean": round(sum(real) / len(real)) if real else 0,
        "rows": rows,
    }


def summarize(args: argparse.Namespace) -> int:
    root: Path = args.root.resolve()
    result = summarize_turns(
        read_turn_events(root / "turns"), peak_prompt_tokens(root / "data" / "audit.db")
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for key, value in result.items():
        if key != "rows":
            print(f"{key}: {value}")
    print("| turn | peak prompt | peak estimate | auto | clear | trim | reads | s |")
    print("|---|---|---|---|---|---|---|---|")
    for r in result["rows"]:
        print(
            f"| {r['turn']} | {r['peak_prompt']} | {r['peak_estimate']} | {r['auto']} |"
            f" {r['clear']} | {r['trim']} | {r['reads']} | {r.get('seconds', '')} |"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="seed a repo and run the session once")
    run_p.add_argument("--clearing", choices=("on", "off"), required=True)
    run_p.add_argument("--root", type=Path, required=True, help="fresh scratch directory")
    run_p.add_argument("--config", type=Path, required=True, help="config.yaml to copy")
    run_p.add_argument("--turns", type=int, default=len(build_prompts()))
    run_p.add_argument("--max-minutes", type=float, default=28.0)
    run_p.add_argument("--slots-url", default="http://127.0.0.1:8009/slots")
    run_p.add_argument("--max-idle-wait", type=float, default=300.0)
    run_p.add_argument("--turn-timeout", type=float, default=600.0)
    sum_p = sub.add_parser("summarize", help="count compactions and peak prompts of a run")
    sum_p.add_argument("root", type=Path)
    sum_p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return run(args) if args.command == "run" else summarize(args)


if __name__ == "__main__":
    sys.exit(main())
