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
import contextlib
import os
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from ..browser import BrowserDriver, MockBrowserDriver
from ..context.manifest import _FALLBACK_IGNORE
from ..context.skills import register_skill_handlers
from ..cu_verify import DriverStateProbe
from ..desktop import DesktopDriver, MockDesktopDriver
from ..desktop.grounding_client import GroundingLocator
from .ask_user import ask_user
from .browser import register_browser_handlers
from .consult import handle_consult
from .delegate import register_delegate_handlers
from .desktop import register_desktop_handlers
from .dispatch import ToolDispatcher
from .exit_plan import exit_plan_mode
from .search import DEFAULT_MAX_RESULTS, STOPPED, glob_crosses_folders, glob_matcher, grep
from .send_file import send_file
from .shell import ShellPolicy, run_shell
from .steering_edit import propose_steering_edit
from .task_list import task_list
from .tool_search import register_tool_search_handler
from .web_search import web_fetch, web_search
from .write import fs_edit, fs_write

if TYPE_CHECKING:
    # sent_files imports artifacts, which imports this package: type-only here.
    from ..sent_files import SentFiles
    from ..session import Session

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


#: Entries fs_list returns before it says it truncated (TD-5102).
FS_LIST_MAX_ENTRIES = 1000


def _list_dir(
    root: Path,
    pattern: str,
    recursive: bool,
    sort: str = "name",
    limit: int = FS_LIST_MAX_ENTRIES,
) -> str:
    """List entries under *root*, filtered by a glob *pattern*.

    Respects the manifest's ignore rules (``node_modules``,
    ``__pycache__``, ``.venv``, ``.git``, ``.tst``).  Output is one
    relative path per line; "(no matches)" when nothing matches.

    *pattern* uses the shared glob dialect (TD-5102): a pattern with a
    ``/`` or ``**`` such as ``src/**/*.ts`` matches the path from *root*
    and implies *recursive*.  *sort* is ``name`` or ``modified`` (newest
    first).  More than *limit* entries are cut, and the cut is stated.
    """
    if not root.exists():
        return f"Error: directory not found: {root}"
    if not root.is_dir():
        return f"Error: '{root}' is not a directory; use fs_read to read a file"
    if sort not in ("name", "modified"):
        return "Error: sort must be 'name' or 'modified'"

    pattern = pattern or "*"
    matches = glob_matcher(pattern)
    if glob_crosses_folders(pattern):
        recursive = True

    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _FALLBACK_IGNORE)
        if not recursive:
            dirnames.clear()  # root-level entries only
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if matches(rel):
                results.append(rel)

    if not results:
        return "(no matches)"
    if sort == "modified":

        def _mtime(rel: str) -> float:
            try:
                return (root / rel).stat().st_mtime
            except OSError:
                return 0.0

        results.sort(key=lambda rel: (-_mtime(rel), rel))
    else:
        results.sort()
    cap = max(1, int(limit or FS_LIST_MAX_ENTRIES))
    if len(results) <= cap:
        return "\n".join(results)
    return "\n".join(results[:cap]) + (
        f"\n… [truncated: showing {cap} of {len(results)} entries; narrow the path or pattern]"
    )


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
    sort: str = "name",
    tool_call_id: str = "",
) -> str:
    """List entries under *path*, filtered by a glob *pattern* (TD-603, TD-5102)."""
    return await asyncio.to_thread(_list_dir, Path(path), pattern, recursive, sort)


async def fs_grep(
    session: object,
    pattern: str,
    path: str,
    glob: str = "",
    case_insensitive: bool = False,
    context: int = 0,
    output_mode: str = "files",
    max_results: int = DEFAULT_MAX_RESULTS,
    tool_call_id: str = "",
) -> str:
    """Search file contents under *path* with a regex (TD-5102).

    Stopping the turn ends the search, as it does a shell command: the
    result comes back at once, and the search thread ends at its next
    line (one line's match is bounded by ``LINE_MATCH_SECONDS``).
    """
    stop = threading.Event()
    search = asyncio.ensure_future(
        asyncio.to_thread(
            partial(
                grep,
                Path(path),
                pattern,
                glob=glob,
                case_insensitive=case_insensitive,
                context=context,
                output_mode=output_mode,
                max_results=max_results,
                stop=stop,
            )
        )
    )
    try:
        wait_for_abort = getattr(session, "wait_for_turn_abort", None)
        if wait_for_abort is None:
            return await search
        abort = asyncio.ensure_future(wait_for_abort())
        try:
            await asyncio.wait({search, abort}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            abort.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await abort
        return search.result() if search.done() else STOPPED
    finally:
        # Also on cancellation: a thread cannot be cancelled, only told.
        stop.set()


async def _shell_under_live_allowlist(
    session: Session | None,
    command: str,
    timeout_secs: int = 30,
    tool_call_id: str = "",
    *,
    allowlist: Callable[[], tuple[str, ...] | None],
) -> str:
    """``run_shell`` with the allowlist read at the call, not at registration."""
    policy = ShellPolicy(allowed_commands=allowlist())
    return await run_shell(session, command, timeout_secs, tool_call_id, policy=policy)


def register_builtin_handlers(
    dispatcher: ToolDispatcher,
    allowed_commands: tuple[str, ...] | None = None,
    desktop_driver: DesktopDriver | None = None,
    browser_driver: BrowserDriver | None = None,
    grounding_client: GroundingLocator | None = None,
    sent_files: SentFiles | None = None,
    shell_allowlist: Callable[[], tuple[str, ...] | None] | None = None,
) -> None:
    """Register the built-in tool handlers on *dispatcher*.

    ``allowed_commands`` restricts the shell tool to the given binaries
    when set (TD-605); ``None`` leaves it unrestricted.  ``shell_allowlist``
    instead reads the allowlist at every call, so a session moved to
    another project (TD-1715) runs under that project's list; it wins over
    ``allowed_commands``.  ``desktop_driver``
    is the process-wide computer-use backend (TD-3301); omitted means the
    in-process mock so every builtin still has a handler.  ``browser_driver``
    is the session browser (TD-1710); omitted is the in-process mock.
    ``grounding_client`` is the optional local click-grounding model
    (TD-3902); omitted or disabled keeps the intended (x, y).
    ``sent_files`` stores what ``send_file`` sends (TD-5143); omitted, the
    handler still exists and refuses with ``send_unavailable``.
    """
    dispatcher.register_handler("fs_read", fs_read)
    dispatcher.register_handler("fs_list", fs_list)
    dispatcher.register_handler("fs_grep", fs_grep)
    dispatcher.register_handler("web_search", web_search)
    dispatcher.register_handler("web_fetch", web_fetch)
    dispatcher.register_handler("fs_write", fs_write)
    dispatcher.register_handler("fs_edit", fs_edit)
    if shell_allowlist is not None:
        shell = partial(_shell_under_live_allowlist, allowlist=shell_allowlist)
    else:
        shell = partial(run_shell, policy=ShellPolicy(allowed_commands=allowed_commands))
    dispatcher.register_handler("shell", shell)
    register_desktop_handlers(
        dispatcher,
        desktop_driver if desktop_driver is not None else MockDesktopDriver(),
        grounding_client=grounding_client,
    )
    register_browser_handlers(
        dispatcher, browser_driver if browser_driver is not None else MockBrowserDriver()
    )
    # TD-709 (dev): the state probe lets an attached verifier snapshot
    # before/after actuation.  Inert until a verifier is wired (config-gated).
    dispatcher.state_probe = DriverStateProbe(
        desktop_driver if desktop_driver is not None else MockDesktopDriver(),
        browser_driver if browser_driver is not None else MockBrowserDriver(),
    )
    register_skill_handlers(dispatcher)
    register_delegate_handlers(dispatcher)
    dispatcher.register_handler("consult", handle_consult)
    dispatcher.register_handler("send_file", partial(send_file, service=sent_files))
    dispatcher.register_handler("propose_steering_edit", propose_steering_edit)
    dispatcher.register_handler("task_list", task_list)
    dispatcher.register_handler("ask_user", ask_user)
    dispatcher.register_handler("exit_plan_mode", exit_plan_mode)
    register_tool_search_handler(dispatcher)
    from ..connectors.github_tools import register_github_handlers

    register_github_handlers(dispatcher)
