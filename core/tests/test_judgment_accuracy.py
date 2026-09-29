"""Recorded-fixture accuracy for the classifier seam (TD-708)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from tstd.autonomy.judgment_eval import format_report, run_recorded_eval

_CORE = Path(__file__).resolve().parents[1]
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "judgment_accuracy.json"
_SCRIPT = _CORE / "scripts" / "eval_judgment_accuracy.py"


def test_recorded_fixture_reports_accuracy() -> None:
    scores = run_recorded_eval(_FIXTURES)
    assert [(score.connector, score.accuracy, score.b_rate) for score in scores] == [
        ("worker", "5/7", "4/7"),
        ("typed", "6/7", "3/7"),
    ]
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--fixtures", str(_FIXTURES)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == format_report(scores)


def test_eval_runner_imports_no_network_client() -> None:
    """The runner scores recordings. It does not import a client."""
    banned = {"httpx", "socket", "websockets", "aiohttp", "urllib"}
    for rel in ("tstd/autonomy/judgment_eval.py", "scripts/eval_judgment_accuracy.py"):
        tree = ast.parse((_CORE / rel).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        assert imported.isdisjoint(banned), rel
