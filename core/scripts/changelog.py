"""Generate a release changelog from commit subjects (TD-1303).

    git tag tstdesk-v0.1.0 && uv run python scripts/changelog.py  # since previous app tag
    uv run python scripts/changelog.py tstdesk-v0.1.0..HEAD       # explicit range

App tags are namespaced ``tstdesk-v*`` (TD-4812) so the tst-cu-mcp package's
plain ``v*`` tags never land in an app release's diff.

Lines are grouped by story id (``TD-123: subject`` convention); commits
without one land under "Other".  Integration merges are skipped — the
story commit already carries the entry.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict


def default_range() -> str:
    tags = subprocess.run(
        ["git", "tag", "--list", "tstdesk-v*", "--sort=-v:refname"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if len(tags) < 2:
        return ""  # first release: whole history
    return f"{tags[1]}..{tags[0]}"


def main(argv: list[str]) -> int:
    rev_range = argv[1] if len(argv) > 1 else default_range()
    cmd = ["git", "log", "--no-merges", "--format=%h %s"]
    if rev_range:
        cmd.append(rev_range)
    log = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.splitlines()

    groups: dict[str, list[str]] = defaultdict(list)
    for line in log:
        short, _, subject = line.partition(" ")
        match = re.match(r"(TD-\d+):\s*(.*)", subject)
        key, text = (match.group(1), match.group(2)) if match else ("Other", subject)
        groups[key].append(f"- {text} ({short})")

    def sort_key(key: str) -> tuple[int, str]:
        num = key.removeprefix("TD-")
        return (int(num) if num.isdigit() else sys.maxsize, key)

    out = ["## What changed", ""]
    for key in sorted(groups, key=sort_key):
        out.append(f"**{key}**" if key != "Other" else "**Other**")
        out.extend(groups[key])
        out.append("")
    print("\n".join(out).rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
