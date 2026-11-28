"""The one switch for the app's own background network calls (DOCS-28, ENTERPRISE-04).

The manager's rule: AGENTS.md's prime directive 3 stays, and the features
change. The two calls the app makes without being asked, the release check
(the host: at launch and every four hours) and the model price refresh (the
daemon: a little after start, then daily), sit behind one setting, on by
default and stated in the README. It is ``automatic`` in the data folder's
``update.yaml``, which Settings, About writes and the host already reads;
this module reads it the same way (missing or unreadable means on).
"""

from __future__ import annotations

from pathlib import Path

import yaml

FILE_NAME = "update.yaml"


def background_checks_on(data_dir: Path) -> bool:
    """Whether the app may make its own background network calls."""
    try:
        raw = yaml.safe_load((data_dir / FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return True
    if not isinstance(raw, dict):
        return True
    value = raw.get("automatic")
    return value if isinstance(value, bool) else True
