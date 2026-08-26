"""Built-in tool handlers — filesystem read tools (TD-603) and shell (TD-605).

The read handlers consume paths that the boundary guard (TD-602) has
already canonicalized and checked: dispatch refuses out-of-workspace or
Windows-unsafe paths before a handler runs, so handlers receive a valid
in-workspace path and only need to deal with file-level concerns —
binary/encoding refusal, line windows, truncation with stated totals,
and ignore-aware listing.

Read handlers are thin async wrappers: the blocking filesystem work runs
in a worker thread via ``asyncio.to_thread`` so the event loop never
stalls (AGENTS.md §6).  The shell handler streams subprocess output
through the event loop's native subprocess support instead (see
``shell.py``).
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from functools import partial
from pathlib import Path

from ..browser import BrowserDriver, MockBrowserDriver
from ..context.manifest import _FALLBACK_IGNORE
from ..context.skills import register_skill_handlers
from ..desktop import DesktopDriver, MockDesktopDriver
from ..desktop.grounding_client import GroundingLocator
from .browser import register_browser_handlers
from .delegate import register_delegate_handlers
from .desktop import register_desktop_handlers
from .dispatch import ToolDispatcher
from .shell import ShellPolicy, run_shell
from .web_search import web_fetch, web_search
from .write import fs_edit, fs_write

# Handler-level cap on formatted lines.  Dispatch additionally caps the
# returned string's byte length with its own truncation marker.
_MAX_READ_LINES = 2000
# Bytes probed at the start of a file for binary detection (NUL).
_BINARY_PROBE = 1024


def _looks_binary(target: Path) -> bool:
    """Whether *target*'s head contains NUL bytes (a binary signature)."""
    with target.open("rb") as f:
        head = f.read(_BINARY_PROBE)
    return b"\x00" in head


def _read_file(target: Path, limit: int, offset: int) -> str:
    """Read *target* with optional line ranges; numbered lines.

    Refuses binary files and non-UTF-8 content with an explanatory
    message rather than dumping bytes.  Output is capped at
    ``_MAX_READ_LINES`` formatted lines; when truncated by the cap, a
    marker states the file's total line count and byte size.
    """
    if not target.exists():
        return f"Error: file not found: {target}"
    if target.is_dir():
        return f"Error: '{target}' is a directory; use fs_list to list it"
    if _looks_binary(target):
        return (
            f"Error: refused to read '{target}' — binary file "
            "(NUL bytes detected in the first chunk)"
        )

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return (
            f"Error: refused to read '{target}' — content is not valid UTF-8 "
            "(possible binary or legacy encoding)"
        )
    except OSError as e:
        return f"Error: could not read '{target}': {e}"

    lines = content.splitlines()
    total = len(lines)
    start = offset - 1 if offset > 0 else 0
    explicit = bool(limit and limit > 0)
    window = limit if explicit else _MAX_READ_LINES
    end = min(start + window, total)
    if start >= total:
        return "(no lines in range)"

    rendered = "\n".join(f"{i}: {line}" for i, line in enumerate(lines[start:end], start=start + 1))
    # A partial window always names the next offset so the model can
    # walk a long file instead of guessing (TD-608). The default cap
    # is not "the whole file" — the schema used to claim it was.
    if end < total:
        nxt = end + 1
        size = target.stat().st_size
        if explicit:
            rendered += (
                f"\n… [window {start + 1}-{end} of {total} lines; continue with offset={nxt}]"
            )
        else:
            rendered += (
                f"\n… [truncated: {total} lines, {size} bytes total; continue with offset={nxt}]"
            )
    return rendered


def _list_dir(root: Path, pattern: str, recursive: bool) -> str:
    """List entries under *root*, filtered by a glob *pattern*.

    Respects the manifest's ignore rules (``node_modules``,
    ``__pycache__``, ``.venv``, ``.git``, ``.tst``).  Output is one
    relative path per line, sorted; "(no matches)" when nothing matches.
    """
    if not root.exists():
        return f"Error: directory not found: {root}"
    if not root.is_dir():
        return f"Error: '{root}' is not a directory; use fs_read to read a file"

    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _FALLBACK_IGNORE)
        if not recursive:
            dirnames.clear()  # root-level entries only
        for name in sorted(filenames):
            if not fnmatch.fnmatchcase(name, pattern):
                continue
            results.append((Path(dirpath) / name).relative_to(root).as_posix())

    if not results:
        return "(no matches)"
    return "\n".join(results)


async def fs_read(
    session: object, path: str, limit: int = 0, offset: int = 0, tool_call_id: str = ""
) -> str:
    """Read a file with optional line ranges; numbered lines (TD-603)."""
    return await asyncio.to_thread(_read_file, Path(path), limit, offset)


async def fs_list(
    session: object,
    path: str,
    pattern: str = "*",
    recursive: bool = False,
    tool_call_id: str = "",
) -> str:
    """List entries under *path*, filtered by a glob *pattern* (TD-603)."""
    return await asyncio.to_thread(_list_dir, Path(path), pattern, recursive)


def register_builtin_handlers(
    dispatcher: ToolDispatcher,
    allowed_commands: tuple[str, ...] | None = None,
    desktop_driver: DesktopDriver | None = None,
    browser_driver: BrowserDriver | None = None,
    grounding_client: GroundingLocator | None = None,
) -> None:
    """Register the built-in tool handlers on *dispatcher*.

    ``allowed_commands`` restricts the shell tool to the given binaries
    when set (TD-605); ``None`` leaves it unrestricted.  ``desktop_driver``
    is the process-wide computer-use backend (TD-3301); omitted means the
    in-process mock so every builtin still has a handler.  ``browser_driver``
    is the session browser (TD-1710); omitted is the in-process mock.
    ``grounding_client`` is the optional local click-grounding model
    (TD-3902); omitted or disabled keeps the intended (x, y).
    """
    dispatcher.register_handler("fs_read", fs_read)
    dispatcher.register_handler("fs_list", fs_list)
    dispatcher.register_handler("web_search", web_search)
    dispatcher.register_handler("web_fetch", web_fetch)
    dispatcher.register_handler("fs_write", fs_write)
    dispatcher.register_handler("fs_edit", fs_edit)
    dispatcher.register_handler(
        "shell", partial(run_shell, policy=ShellPolicy(allowed_commands=allowed_commands))
    )
    register_desktop_handlers(
        dispatcher,
        desktop_driver if desktop_driver is not None else MockDesktopDriver(),
        grounding_client=grounding_client,
    )
    register_browser_handlers(
        dispatcher, browser_driver if browser_driver is not None else MockBrowserDriver()
    )
    register_skill_handlers(dispatcher)
    register_delegate_handlers(dispatcher)
