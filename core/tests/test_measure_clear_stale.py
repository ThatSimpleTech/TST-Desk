"""The TD-5162 measurement script (``scripts/measure_clear_stale.py``).

The live run needs a model server, so only the parts that decide the
numbers are tested here: which compactions count, where each turn's peak
prompt comes from, what the scratch config changes, and that the script
never stops a daemon of another data dir.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tstd.audit import AuditStore

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_clear_stale.py"


@pytest.fixture(scope="module")
def measure() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_measure_clear_stale", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compacted(seq: int, trigger: str, dropped: int = 1) -> dict[str, Any]:
    return {
        "type": "context_compacted",
        "session_id": "s1",
        "seq": seq,
        "trigger": trigger,
        "dropped_messages": dropped,
        "kept_messages": 4,
        "tokens_before": 50_000,
        "tokens_after": 20_000,
    }


def _usage(seq: int, used: int) -> dict[str, Any]:
    return {
        "type": "context_usage",
        "session_id": "s1",
        "seq": seq,
        "tier": "brain",
        "used_tokens": used,
        "context_window": 65_536,
        "compact_at": 45_875,
    }


def _read(seq: int, path: str) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "session_id": "s1",
        "seq": seq,
        "tool_call_id": f"c{seq}",
        "name": "fs_read",
        "arguments": {"path": path},
    }


def _done(seq: int) -> dict[str, Any]:
    return {"type": "turn_complete", "session_id": "s1", "seq": seq, "duration": 12.34}


def _write_turns(turns_dir: Path, turns: list[list[dict[str, Any]]]) -> None:
    turns_dir.mkdir(parents=True)
    for n, events in enumerate(turns, start=1):
        lines = "".join(json.dumps(e) + "\n" for e in events)
        (turns_dir / f"turn-{n:02d}.ndjson").write_text(lines, encoding="utf-8")


def test_only_auto_compactions_count_as_compactions(measure: ModuleType, tmp_path: Path) -> None:
    """Trims and clears remove no turn; they are counted apart (TD-5150, TD-5115)."""
    turns = [
        [_usage(1, 30_000), _read(2, "src/a.py"), _read(3, "src/b.py"), _done(4)],
        [
            _usage(5, 46_000),
            _compacted(6, "clear_stale", dropped=5),
            _usage(7, 25_000),
            _compacted(8, "inflight_trim", dropped=2),
            _read(9, "src/a.py"),
            _done(10),
        ],
        [_usage(11, 47_000), _compacted(12, "auto"), _usage(13, 9_000), _done(14)],
    ]
    _write_turns(tmp_path / "turns", turns)

    result = measure.summarize_turns(measure.read_turn_events(tmp_path / "turns"), [])

    assert result["compactions"] == 1
    assert result["clears"] == 1
    assert result["results_cleared"] == 5
    assert result["inflight_trims"] == 1
    assert result["fs_reads"] == 3
    assert result["rereads"] == 1
    assert [r["auto"] for r in result["rows"]] == [0, 0, 1]
    assert [r["peak_estimate"] for r in result["rows"]] == [30_000, 46_000, 47_000]
    assert result["sessions"] == 1


def test_a_replayed_event_is_counted_once(measure: ModuleType, tmp_path: Path) -> None:
    """A turn's stream that repeats an earlier seq does not count it twice."""
    _write_turns(
        tmp_path / "turns",
        [[_compacted(1, "auto"), _done(2)], [_compacted(1, "auto"), _done(3)]],
    )

    result = measure.summarize_turns(measure.read_turn_events(tmp_path / "turns"), [])

    assert result["compactions"] == 1


def test_peak_prompt_is_the_largest_real_turn_call(measure: ModuleType, tmp_path: Path) -> None:
    """Provider usage per turn from audit.db; classifier calls are not the turn's."""
    db = tmp_path / "audit.db"
    store = AuditStore(db)
    store.append_session("s1", "/w", 1.0)
    first = store.append_turn("s1", 0, "brain", 0, 0.0, 1.0, ts=10.0)
    second = store.append_turn("s1", 1, "worker", 0, 0.0, 1.0, ts=20.0)
    store.append_model_call("s1", first, "brain", "m", 12_000, 0, 50, 0.0, ts=5.0)
    store.append_model_call("s1", first, "brain", "m", 18_500, 12_000, 70, 0.0, ts=6.0)
    store.append_model_call("s1", None, "worker", "m", 90_000, 0, 1, 0.0, True, ts=7.0)
    store.append_model_call("s1", second, "worker", "m", 21_000, 18_000, 40, 0.0, ts=15.0)
    store.close()

    assert measure.peak_prompt_tokens(db) == [18_500, 21_000]
    assert measure.peak_prompt_tokens(tmp_path / "missing.db") == []


def test_scratch_config_changes_only_the_switch_and_log_size(measure: ModuleType) -> None:
    config: dict[str, Any] = {
        "active_preset": "tst-default",
        "clear_tool_results": {"enabled": True, "keep_recent_tool_results": 5},
        "session": {"send_file_max_bytes": 10},
    }

    off = measure.scratch_config(config, clearing=False)
    on = measure.scratch_config({"active_preset": "tst-default"}, clearing=True)

    assert off["clear_tool_results"] == {"enabled": False, "keep_recent_tool_results": 5}
    assert off["session"]["send_file_max_bytes"] == 10
    assert off["session"]["log_max_events"] >= 100_000
    assert off["active_preset"] == "tst-default"
    assert config["clear_tool_results"]["enabled"] is True
    assert on["clear_tool_results"] == {"enabled": True}


def test_prompts_read_every_seed_file_and_recall_an_earlier_one(measure: ModuleType) -> None:
    prompts = measure.build_prompts()
    reads = [p for p in prompts if p.startswith("Read ")]
    recalls = [p for p in prompts if p.startswith("Back to ")]

    assert len(prompts) == 24 and len(reads) == 20 and len(recalls) == 4
    for rel in measure.SEED_FILES:
        assert sum(f"src/{rel} " in p for p in reads) == 1
    for n, prompt in enumerate(prompts):
        if prompt.startswith("Back to "):
            assert (n + 1) % measure.RECALL_EVERY == 0
            target = prompt.split()[2].rstrip(",")
            assert any(target in p for p in prompts[:n])


def test_seed_repo_commits_the_seed_files(measure: ModuleType, tmp_path: Path) -> None:
    measure.seed_repo(tmp_path / "repo")

    tracked = subprocess.run(
        ["git", "-C", str(tmp_path / "repo"), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(tracked) == sorted(f"src/{rel}" for rel in measure.SEED_FILES)


def test_stop_daemon_leaves_another_data_dirs_daemon_alone(
    measure: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A port file naming a process that is not this data dir's daemon is not signalled."""
    (tmp_path / "port.json").write_text(json.dumps({"pid": os.getpid(), "port": 1, "token": "t"}))
    signalled: list[int] = []
    monkeypatch.setattr(measure.os, "kill", lambda pid, sig: signalled.append(pid))

    measure.stop_daemon(tmp_path)

    assert signalled == []


def test_stop_daemon_stops_its_own_data_dirs_daemon(measure: ModuleType, tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(tmp_path)])
    try:
        (tmp_path / "port.json").write_text(json.dumps({"pid": child.pid, "port": 1}))
        measure.stop_daemon(tmp_path)
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        child.kill()


def test_a_turn_with_no_clean_turn_complete_counts_as_failed(
    measure: ModuleType, tmp_path: Path
) -> None:
    """A turn the timeout cut off never sends turn_complete; one with an error_code failed."""
    errored = {**_done(4), "error_code": "provider_error"}
    _write_turns(tmp_path / "turns", [[_usage(1, 10)], [_done(2)], [_usage(3, 10), errored]])

    result = measure.summarize_turns(measure.read_turn_events(tmp_path / "turns"), [])

    assert [r["failed"] for r in result["rows"]] == [True, False, True]
    assert result["failed_turns"] == 2


def test_peaks_pair_with_the_turns_that_completed(measure: ModuleType, tmp_path: Path) -> None:
    """A turn with no audit row (cut off) does not shift the later turns' peaks."""
    _write_turns(tmp_path / "turns", [[_done(1)], [_usage(2, 10)], [_done(3)]])

    result = measure.summarize_turns(measure.read_turn_events(tmp_path / "turns"), [100, 300])

    assert [r["peak_prompt"] for r in result["rows"]] == [100, None, 300]
    assert result["peak_prompt_max"] == 300


def test_a_turn_with_no_model_call_keeps_its_place(measure: ModuleType, tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    store = AuditStore(db)
    store.append_session("s1", "/w", 1.0)
    first = store.append_turn("s1", 0, "brain", 0, 0.0, 1.0, ts=10.0)
    store.append_turn("s1", 1, "brain", 0, 0.0, 1.0, ts=20.0)
    third = store.append_turn("s1", 2, "brain", 0, 0.0, 1.0, ts=30.0)
    store.append_model_call("s1", first, "brain", "m", 1_000, 0, 5, 0.0, ts=5.0)
    store.append_model_call("s1", third, "brain", "m", 3_000, 0, 5, 0.0, ts=25.0)
    store.close()

    assert measure.peak_prompt_tokens(db) == [1_000, None, 3_000]
