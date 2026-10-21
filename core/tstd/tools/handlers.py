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
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from ..attachments import DEFAULT_MAX_FILE_BYTES, detect_image_mime, format_bytes
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
from .read_notebook import render_notebook
from .read_pdf import PDF_MAX_PAGE_IMAGES, PDF_MAX_PAGES, PagesError, looks_like_pdf, read_pdf
from .results import DEFAULT_MAX_RESULT_CHARS, HandlerOutput, ToolImage
from .search import DEFAULT_MAX_RESULTS, STOPPED, glob_crosses_folders, glob_matcher, grep
from .send_file import send_file
from .session_search import SessionRecall, register_session_search_handler
from .shell import OnTimeout, ShellPolicy, run_shell
from .shell_job_tools import shell_job_output, shell_job_stop
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


#: Largest image file ``fs_read`` sends as a vision part, the same cap
#: as an attached image (TD-4705). Base64 adds a third on the wire.
FS_READ_MAX_IMAGE_BYTES = DEFAULT_MAX_FILE_BYTES


def _read_head(target: Path) -> bytes:
    with target.open("rb") as f:
        return f.read(_BINARY_PROBE)


def _read_image(target: Path, mime: str, *, vision: bool) -> str | HandlerOutput:
    """An image file as a vision part, or a plain refusal (TD-5130)."""
    size = target.stat().st_size
    what = f"'{target}' is an image ({mime}, {format_bytes(size)})"
    if not vision:
        return (
            f"Error: {what}, but the model reading this result does not accept "
            "images, so it was not opened. Set vision: true on a tier whose model "
            "takes images, or describe what you need from it another way."
        )
    if size > FS_READ_MAX_IMAGE_BYTES:
        return (
            f"Error: {what}, over the {format_bytes(FS_READ_MAX_IMAGE_BYTES)} "
            "limit for an image read, so it was not opened."
        )
    with target.open("rb") as f:
        data = f.read(FS_READ_MAX_IMAGE_BYTES + 1)
    # Checked again on the bytes sent: the file may have changed since the probe.
    if len(data) > FS_READ_MAX_IMAGE_BYTES or detect_image_mime(data) != mime:
        return f"Error: '{target}' changed while it was read; read it again."
    return HandlerOutput(
        text=f"Image: {target} ({mime}, {format_bytes(len(data))}). It follows as an image.",
        images=(ToolImage(mime, data),),
    )


#: Room kept under the result cap for the notes after the page text.
_PDF_NOTE_ROOM = 600


def _read_pdf_file(
    target: Path,
    pages: str,
    page_images: bool,
    *,
    vision: bool,
    vision_slots: int,
    max_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> str | HandlerOutput:
    """PDF pages as text, with page images when asked on a vision tier.

    The text stops at whole pages under *max_chars* (the dispatcher's cap
    for this call), so the cap never cuts a page in half or drops the note
    naming the next range: offset/limit do not page a PDF, ``pages`` does.
    """
    slots = vision_slots if page_images and vision else 0
    try:
        doc = read_pdf(target, pages, image_slots=slots, max_image_bytes=FS_READ_MAX_IMAGE_BYTES)
    except PagesError as e:
        return f"Error: {e}"
    except OSError as e:
        return f"Error: could not read PDF '{target}': {e}"
    if doc.page_count == 0:
        return f"PDF: {target} has no pages."
    noun = "page" if doc.page_count == 1 else "pages"
    budget = max(0, (max_chars or DEFAULT_MAX_RESULT_CHARS) - _PDF_NOTE_ROOM)
    blocks: list[str] = []
    used = 0
    last = doc.first - 1
    cut_page: int | None = None
    for number, text in doc.texts:
        block = f"── page {number} ──\n" + (
            text if text.strip() else "(no text on this page; it may be a scan)"
        )
        if used + len(block) + 1 > budget:
            if not blocks:
                # Not even one page fits: send what does, and say so.
                blocks.append(block[: max(0, budget - 1)])
                last = number
                cut_page = number
            break
        blocks.append(block)
        used += len(block) + 1
        last = number
    head = f"PDF: {target}, {doc.page_count} {noun}; pages {doc.first}-{last} below."
    body: list[str] = [head, *blocks]
    if cut_page is not None:
        body.append(
            f"[page {cut_page}'s text was cut: it is longer than one result holds "
            f"({max_chars} characters)]"
        )
    if last < doc.page_count:
        why = "" if last == doc.last else "; the rest did not fit in one result"
        body.append(
            f"… [pages {doc.first}-{last} of {doc.page_count}{why}; "
            f'continue with pages="{last + 1}-{min(doc.page_count, last + PDF_MAX_PAGES)}"]'
        )
    # Images only of pages whose text is in this result.
    shown = [(n, img) for n, img in zip(doc.imaged, doc.images, strict=False) if n <= last]
    too_large = [n for n in doc.too_large if n <= last]
    if page_images:
        if not vision:
            body.append(
                "[page images were not made: the model reading this result does not accept images]"
            )
        else:
            if shown:
                listed = ", ".join(str(n) for n, _ in shown)
                body.append(f"[images of page {listed} follow, in that order]")
            if too_large:
                listed = ", ".join(str(n) for n in too_large)
                body.append(f"[page {listed} rendered too large to send as an image]")
            skipped = (last - doc.first + 1) - len(shown) - len(too_large)
            if skipped > 0:
                body.append(
                    f"[{skipped} more page(s) in the range were not imaged: one result "
                    f"carries at most {min(PDF_MAX_PAGE_IMAGES, vision_slots)} page image(s)]"
                )
    text = "\n".join(body)
    if shown:
        return HandlerOutput(text=text, images=tuple(img for _, img in shown))
    return text


def _window(lines: list[str], limit: int, offset: int, total_note: str) -> str:
    """Numbered *lines* from *offset*, at most *limit* (default cap)."""
    total = len(lines)
    start = offset - 1 if offset > 0 else 0
    explicit = bool(limit and limit > 0)
    window = limit if explicit else _MAX_READ_LINES
    end = min(start + window, total)
    if start >= total:
        return "(no lines in range)"
    rendered = "\n".join(f"{i}: {line}" for i, line in enumerate(lines[start:end], start=start + 1))
    if end < total:
        nxt = end + 1
        if explicit:
            rendered += (
                f"\n… [window {start + 1}-{end} of {total} lines; continue with offset={nxt}]"
            )
        else:
            rendered += f"\n… [truncated: {total} lines{total_note}; continue with offset={nxt}]"
    return rendered


def _read_any(
    target: Path,
    limit: int,
    offset: int,
    pages: str,
    page_images: bool,
    *,
    vision: bool,
    vision_slots: int,
    max_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> str | HandlerOutput:
    """Route a read by what the file is, from its bytes (TD-5130)."""
    if not target.exists():
        return f"Error: file not found: {target}"
    if target.is_dir():
        return f"Error: '{target}' is a directory; use fs_list to list it"
    try:
        head = _read_head(target)
    except OSError as e:
        return f"Error: could not read '{target}': {e}"
    mime = detect_image_mime(head)
    if mime is not None:
        return _read_image(target, mime, vision=vision)
    if looks_like_pdf(head, target):
        return _read_pdf_file(
            target,
            pages,
            page_images,
            vision=vision,
            vision_slots=vision_slots,
            max_chars=max_chars,
        )
    if target.suffix.lower() == ".ipynb" and b"\x00" not in head:
        try:
            raw = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            raw = ""
        view = render_notebook(raw) if raw else None
        if view is not None:
            header, lines = view
            return header + "\n" + _window(lines, limit, offset, "")
    return _read_file(target, limit, offset)


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
    session: object,
    path: str,
    limit: int = 0,
    offset: int = 0,
    pages: str = "",
    page_images: bool = False,
    tool_call_id: str = "",
) -> str | HandlerOutput:
    """Read a file (TD-603): text as numbered lines, or media (TD-5130).

    An image goes back as a vision part when the model reading the result
    takes images; a PDF as page text, plus page images on request; a
    notebook as cells and text outputs. The session says whether the
    model has vision (``tool_vision``) and how many images a prompt keeps
    (``tool_max_images``); a session without them is treated as text-only.
    Called directly it fits a PDF under the default result cap; the
    registered handler passes the dispatcher's cap for the call.
    """
    return await _fs_read(
        session, path, limit, offset, pages, page_images, max_chars=DEFAULT_MAX_RESULT_CHARS
    )


async def _fs_read(
    session: object,
    path: str,
    limit: int,
    offset: int,
    pages: str,
    page_images: bool,
    *,
    max_chars: int,
) -> str | HandlerOutput:
    max_images = getattr(session, "tool_max_images", 1)
    # A negative cap is unlimited; 0 keeps no image, so nothing can be shown.
    slots = max_images if isinstance(max_images, int) and max_images >= 0 else PDF_MAX_PAGE_IMAGES
    vision = bool(getattr(session, "tool_vision", False)) and slots > 0
    return await asyncio.to_thread(
        _read_any,
        Path(path),
        limit,
        offset,
        pages,
        page_images,
        vision_slots=slots if vision else 0,
        vision=vision,
        max_chars=max_chars,
    )


def _fs_read_handler(
    dispatcher: ToolDispatcher,
) -> Callable[..., Awaitable[str | HandlerOutput]]:
    """fs_read bound to *dispatcher*, so a PDF fits the cap it applies (TD-5130)."""

    async def handler(
        session: object,
        path: str,
        limit: int = 0,
        offset: int = 0,
        pages: str = "",
        page_images: bool = False,
        tool_call_id: str = "",
    ) -> str | HandlerOutput:
        # Read per call: the loop lowers the cap for a small tier (TD-4839).
        return await _fs_read(
            session,
            path,
            limit,
            offset,
            pages,
            page_images,
            max_chars=dispatcher.max_result_chars or DEFAULT_MAX_RESULT_CHARS,
        )

    return handler


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
    background: bool = False,
    on_timeout: OnTimeout = "kill",
) -> str:
    """``run_shell`` with the allowlist read at the call, not at registration."""
    policy = ShellPolicy(allowed_commands=allowlist())
    return await run_shell(
        session,
        command,
        timeout_secs,
        tool_call_id,
        policy=policy,
        background=background,
        on_timeout=on_timeout,
    )


def register_builtin_handlers(
    dispatcher: ToolDispatcher,
    allowed_commands: tuple[str, ...] | None = None,
    desktop_driver: DesktopDriver | None = None,
    browser_driver: BrowserDriver | None = None,
    grounding_client: GroundingLocator | None = None,
    sent_files: SentFiles | None = None,
    shell_allowlist: Callable[[], tuple[str, ...] | None] | None = None,
    session_recall: SessionRecall | None = None,
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
    ``session_recall`` backs ``session_search`` (TD-5129); omitted, that
    handler refuses with ``session_search_unavailable``.
    """
    dispatcher.register_handler("fs_read", _fs_read_handler(dispatcher))
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
    dispatcher.register_handler("shell_job_output", shell_job_output)
    dispatcher.register_handler("shell_job_stop", shell_job_stop)
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
    register_session_search_handler(dispatcher, session_recall)
    from .lsp import register_lsp_handlers

    register_lsp_handlers(dispatcher)
    from ..connectors.github_tools import register_github_handlers

    register_github_handlers(dispatcher)
