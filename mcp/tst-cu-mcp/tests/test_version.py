"""The version lives in two files and they must agree."""

from __future__ import annotations

import tomllib
from pathlib import Path

from tst_cu_mcp import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_pyproject_and_module_versions_match() -> None:
    with PYPROJECT.open("rb") as handle:
        declared = tomllib.load(handle)["project"]["version"]
    assert declared == __version__


def test_pyobjc_dependencies_are_macos_only() -> None:
    """The bug that kept this server off Windows entirely.

    Three pyobjc pins carried no environment marker, so resolution failed on
    Windows before any code ran. Without a marker on every one of them, `uv sync`
    goes back to being impossible on a platform this server now supports.
    """
    with PYPROJECT.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]

    pyobjc = [d for d in dependencies if d.startswith("pyobjc")]
    assert pyobjc, "expected pyobjc dependencies to still be declared for macOS"
    for dependency in pyobjc:
        assert "sys_platform == 'darwin'" in dependency, dependency


def test_no_windows_runtime_dependency_was_added() -> None:
    """The Windows backend is ctypes and Pillow: stdlib plus what macOS already needed.

    If a Windows-only package ever becomes a hard requirement, that is a decision
    worth making deliberately rather than discovering in a lock file.
    """
    with PYPROJECT.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]

    unmarked = {d.split(";")[0].split("==")[0].strip() for d in dependencies if ";" not in d}
    assert unmarked == {"mcp", "pillow", "pyyaml"}
