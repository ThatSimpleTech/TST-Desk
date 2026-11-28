"""Destructive shell commands outside the workspace rate dangerous (APPROVE-02).

Every shell command asks. A command that deletes, moves over, truncates
or re-permissions something outside the workspace, or runs with raised
privileges, is Class C: the card shows it as dangerous and never offers
Always allow. The same command inside the workspace keeps the B floor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.autonomy import Boundary, DecisionClassifier, DecisionRequest
from tstd.autonomy.destructive import read_danger

WS = Path("/work/proj")


def _rate(command: str) -> tuple[str, str]:
    request = DecisionRequest(tool_name="shell", arguments={"command": command}, is_mutation=True)
    decision = DecisionClassifier(Boundary(workspace_root=WS)).classify(request)
    assert decision.decision_class is not None
    return decision.decision_class.value, decision.rule.id if decision.rule else ""


DANGEROUS = [
    "rm -rf /",
    "rm -rf /home/someone",
    "rm -rf ~",
    "rm -rf ~/projects",
    "rm -rf $HOME/.cache",
    "rm -rf ..",
    "rm -rf ../sibling",
    "rm -rf .",
    "cd src && rm -f /etc/hosts",
    "ls; rm -r /var/lib/thing",
    "mv build /usr/local/bin/tool",
    "cp evil.so /usr/lib/libc.so.6",
    "truncate -s 0 /var/log/syslog",
    "chmod -R 777 /etc",
    "chown -R me /opt",
    "dd if=/dev/zero of=/dev/sda bs=1M",
    "find / -name '*.log' -delete",
    "find /tmp -type f -exec rm {} +",
    "shred -u /home/someone/.ssh/id_ed25519",
    "sudo make install",
    "sudo -E npm i -g thing",
    "doas apt install foo",
    "FOO=1 env BAR=2 rm -rf /srv",
    "nohup rm -rf /data &",
    "mkfs.ext4 /dev/sdb1",
    "rm -- -rf /etc",
]

SAFE = [
    "rm -rf build",
    "rm -rf ./node_modules dist",
    "rm src/old.py",
    "mv a.txt b.txt",
    "cp template.md docs/new.md",
    "find . -name '*.pyc' -delete",
    "chmod +x scripts/run.sh",
    "npm test",
    "echo rm -rf /",
    "git rm -r --cached /etc",
]


@pytest.mark.parametrize("command", DANGEROUS)
def test_dangerous_commands_are_class_c(command: str) -> None:
    assert _rate(command) == ("C", "shell-dangerous")


@pytest.mark.parametrize("command", SAFE)
def test_in_workspace_commands_keep_the_floor(command: str) -> None:
    cls, rule = _rate(command)
    assert rule != "shell-dangerous"
    assert cls == "B"


def test_an_unparseable_line_is_not_judged_here() -> None:
    assert read_danger("rm -rf '/unterminated").targets == []
    assert _rate("rm -rf '/unterminated")[0] == "B"


def test_redirect_words_are_not_targets() -> None:
    assert read_danger("rm -f old.txt > /tmp/log").targets == ["old.txt"]
