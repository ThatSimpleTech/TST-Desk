"""A damaged computer-use policy reads as Off and is never overwritten (CU-20)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tstd.cu_policy import CuPolicy, load_cu_policy, save_cu_policy

GOOD = "actuation:\n  enabled: false\nscoping:\n  denied_apps: [Slack, 1Password]\n"


def _write(tmp_path: Path, text: str) -> Path:
    target = tmp_path / "config.yaml"
    target.write_text(text, encoding="utf-8")
    return target


def test_a_missing_file_is_a_fresh_install(tmp_path: Path) -> None:
    policy = load_cu_policy(tmp_path / "config.yaml")
    assert policy.enabled and policy.damaged is None


@pytest.mark.parametrize(
    "text",
    [
        GOOD + "  oops: [\n",  # a stray bracket
        "- a\n- list\n",
        "actuation:\n  enabled:\n",  # null
        "actuation:\n  enabled: maybe\n",
        "actuation:\n  enabled: 7\n",
        "scoping:\n  denied_apps: {Slack: true}\n",
    ],
)
def test_anything_unclear_reads_as_off_with_a_reason(tmp_path: Path, text: str) -> None:
    policy = load_cu_policy(_write(tmp_path, text))
    assert policy.enabled is False
    assert policy.damaged


@pytest.mark.parametrize(
    ("raw", "want"), [("0", False), ("1", True), ("off", False), ("yes", True)]
)
def test_plain_flags_are_read(tmp_path: Path, raw: str, want: bool) -> None:
    policy = load_cu_policy(_write(tmp_path, f"actuation:\n  enabled: {raw}\n"))
    assert (policy.enabled, policy.damaged) == (want, None)


def test_a_good_file_still_loads(tmp_path: Path) -> None:
    policy = load_cu_policy(_write(tmp_path, GOOD))
    assert policy == CuPolicy(enabled=False, denied_apps=("Slack", "1Password"))


def test_the_next_save_keeps_the_damaged_file(tmp_path: Path) -> None:
    broken = GOOD + "  oops: [\n"
    target = _write(tmp_path, broken)
    save_cu_policy(CuPolicy(enabled=False), target)
    [kept] = list(tmp_path.glob("config.yaml.damaged-*"))
    assert kept.read_text(encoding="utf-8") == broken
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["actuation"] == {"enabled": False}
    assert load_cu_policy(target).damaged is None


def test_a_good_file_is_not_set_aside(tmp_path: Path) -> None:
    target = _write(tmp_path, GOOD)
    save_cu_policy(CuPolicy(enabled=True), target)
    assert not list(tmp_path.glob("*.damaged-*"))
