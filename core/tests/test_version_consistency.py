"""Version consistency across host, daemon, and UI (TD-1303).

One version for the whole product: the Tauri bundle metadata, the crate,
the Python package, and the UI package must agree, because a release tag
names one thing.  This test is the release pipeline's early tripwire —
the release workflow refuses a tag that disagrees with these files.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _toml_version(path: Path) -> str:
    """Top-of-file ``version = "..."`` from a PEP 621 / Cargo manifest."""
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    assert match, f"no version field in {path}"
    return match.group(1)


def test_versions_agree() -> None:
    versions = {
        "core/pyproject.toml": _toml_version(ROOT / "core" / "pyproject.toml"),
        "shell/Cargo.toml": _toml_version(ROOT / "shell" / "Cargo.toml"),
        "shell/tauri.conf.json": json.loads(
            (ROOT / "shell" / "tauri.conf.json").read_text(encoding="utf-8")
        )["version"],
        "ui/package.json": json.loads((ROOT / "ui" / "package.json").read_text(encoding="utf-8"))[
            "version"
        ],
    }
    distinct = set(versions.values())
    assert len(distinct) == 1, "version drift: " + ", ".join(
        f"{name}={v}" for name, v in versions.items()
    )
