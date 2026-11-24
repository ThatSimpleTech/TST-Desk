"""A stdio MCP server's environment is an allow-list (TD-5304).

TD-5267 removed secret-shaped names from the daemon's environment; a name
nobody thought of still went to every server. Now a server gets the MCP
SDKs' list for its operating system, locale, time zone, temp folder and a
password-free proxy, then the names it asks for, then its own values.
"""

from __future__ import annotations

import pytest

from tstd.config import McpServerConfig
from tstd.mcp.child_env import SDK_POSIX, SDK_WINDOWS, child_env

POSIX = {
    "HOME": "/home/me",
    "PATH": "/usr/bin",
    "USER": "me",
    "SHELL": "/bin/bash",
    "LANG": "en_US.UTF-8",
    "LC_TIME": "en_GB.UTF-8",
    "TMPDIR": "/tmp/me",
    "https_proxy": "http://proxy.example:3128",
    "HTTP_PROXY": "http://me:hunter2@proxy.example:3128",
    "OPENAI_API_KEY": "sk-not-for-a-server",
    "NODE_OPTIONS": "--max-old-space-size=4096",
    "DISPLAY": ":0",
    "BASH_FUNC_x%%": "() { echo hi; }",
    "TERM": "() { evil; }",
}


def test_the_base_list_is_the_sdks() -> None:
    assert SDK_POSIX == ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")
    assert "SYSTEMROOT" in SDK_WINDOWS and "PROGRAMFILES(X86)" in SDK_WINDOWS


def test_posix_gets_the_ordinary_ones_only() -> None:
    env = child_env(environ=POSIX, windows=False)
    assert env == {
        "HOME": "/home/me",
        "PATH": "/usr/bin",
        "USER": "me",
        "SHELL": "/bin/bash",
        "LANG": "en_US.UTF-8",
        "LC_TIME": "en_GB.UTF-8",
        "TMPDIR": "/tmp/me",
        "https_proxy": "http://proxy.example:3128",
    }


def test_named_ones_pass_whatever_they_look_like_and_own_values_win() -> None:
    env = child_env(
        ["NODE_OPTIONS", "OPENAI_API_KEY", "MISSING"],
        {"NODE_OPTIONS": "--own", "TOKEN": "its-own"},
        environ=POSIX,
        windows=False,
    )
    assert env["OPENAI_API_KEY"] == "sk-not-for-a-server"
    assert env["NODE_OPTIONS"] == "--own"
    assert env["TOKEN"] == "its-own"
    assert "MISSING" not in env and "DISPLAY" not in env


def test_an_exported_function_is_never_passed() -> None:
    env = child_env(["BASH_FUNC_x%%", "TERM"], environ=POSIX, windows=False)
    assert "TERM" not in env and "BASH_FUNC_x%%" not in env


def test_windows_names_match_in_any_case() -> None:
    windows = {
        "Path": r"C:\\Windows",
        "SystemRoot": r"C:\\Windows",
        "ProgramFiles(x86)": r"C:\\Program Files (x86)",
        "USERPROFILE": r"C:\\Users\\me",
        "Tmp": r"C:\\Temp",
        "JAVA_HOME": r"C:\\jdk",
        "GITHUB_TOKEN": "ghp-x",
        "LANG": "en_US",
    }
    env = child_env(["java_home"], environ=windows, windows=True)
    assert env == {
        "Path": r"C:\\Windows",
        "SystemRoot": r"C:\\Windows",
        "ProgramFiles(x86)": r"C:\\Program Files (x86)",
        "USERPROFILE": r"C:\\Users\\me",
        "Tmp": r"C:\\Temp",
        "JAVA_HOME": r"C:\\jdk",
    }


@pytest.mark.parametrize(
    "names", [["JAVA_HOME", "NODE_OPTIONS"], ["PROGRAMFILES(X86)"], ["_x1"], None]
)
def test_inherit_env_takes_variable_names(names: list[str] | None) -> None:
    spec = McpServerConfig(transport="stdio", command=["x"], inherit_env=names)
    assert spec.inherit_env == (names or [])


@pytest.mark.parametrize("bad", [["A=B"], ["1ABC"], ["has space"], ["$(id)"], "JAVA_HOME"])
def test_inherit_env_refuses_anything_else(bad: object) -> None:
    with pytest.raises(ValueError, match="inherit_env"):
        McpServerConfig(transport="stdio", command=["x"], inherit_env=bad)


def test_duplicates_collapse() -> None:
    spec = McpServerConfig(transport="stdio", command=["x"], inherit_env=["A", " A", "B"])
    assert spec.inherit_env == ["A", "B"]
