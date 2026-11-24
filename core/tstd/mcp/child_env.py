"""The environment a stdio MCP server starts with: an allow-list (TD-5304).

TD-5267 scrubbed secret-shaped names out of the daemon's environment
before a server got it. That is a deny-list: a name nobody thought of got
through. A server now gets only the ordinary variables a program needs to
start, the names this server asks for in its own ``inherit_env``, and its
own values (``env``, TD-5110), last.

The base list is the MCP SDKs' own (``getDefaultEnvironment`` in the
TypeScript SDK, ``DEFAULT_INHERITED_ENV_VARS`` in the Python SDK), the
environment every SDK-based client already starts servers with on each
operating system, plus the locale, the time zone, the temp folder, and a
proxy setting that carries no password. A value that is an exported shell
function is never passed, as the SDKs do.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping, Sequence

from ..tools.shell import credential_in_url

#: The SDKs' list, by operating system.
SDK_WINDOWS: tuple[str, ...] = (
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
)
SDK_POSIX: tuple[str, ...] = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")

#: Added here: text encoding and time, where temporary files go.
EXTRA_WINDOWS: tuple[str, ...] = ("TMP", "TZ")
EXTRA_POSIX: tuple[str, ...] = ("LANG", "LANGUAGE", "TMPDIR", "TZ")
#: Every ``LC_*`` locale variable on POSIX.
_LOCALE_RE = re.compile(r"LC_[A-Z]+")
#: A proxy is passed when its value carries no user:password.
PROXIES: tuple[str, ...] = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")

#: What ``inherit_env`` may name: an ordinary variable name, and the one
#: Windows form with parentheses.
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\(X86\))?")


def base_names(windows: bool) -> tuple[str, ...]:
    """The names every server may get on this kind of system."""
    return (SDK_WINDOWS + EXTRA_WINDOWS) if windows else (SDK_POSIX + EXTRA_POSIX)


def child_env(
    inherit: Sequence[str] = (),
    own: Mapping[str, str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> dict[str, str]:
    """A stdio server's environment: the base list, *inherit*, then *own*.

    A name in *inherit* is passed whatever it looks like: the person named
    it for this server. Windows names match whatever their case.
    """
    source = os.environ if environ is None else environ
    win = sys.platform == "win32" if windows is None else windows
    by_name = {name.upper(): name for name in source} if win else {n: n for n in source}

    def lookup(name: str) -> tuple[str, str] | None:
        actual = by_name.get(name.upper() if win else name)
        return None if actual is None else (actual, source[actual])

    env: dict[str, str] = {}

    def keep(name: str) -> None:
        found = lookup(name)
        if found is not None and not found[1].startswith("()"):
            env[found[0]] = found[1]

    for name in base_names(win):
        keep(name)
    if not win:
        for name in source:
            if _LOCALE_RE.fullmatch(name):
                keep(name)
    for proxy in PROXIES:
        for name in (proxy, proxy.lower()):
            found = lookup(name)
            if found is not None and not credential_in_url(found[1]):
                keep(name)
    for name in inherit:
        keep(name)
    env.update(own or {})
    return env
