"""Files read again after a compaction (TD-5128).

A compaction folds whole turns, and with them the ``fs_read`` results and
the edits the agent made. The agent then works from a summary that names
those files but no longer has their text, so it reads them again, one
call each. Instead, the files read or changed in the folded turns are
read from disk now, newest first, and ride under the summary within a
budget of files and tokens.

What may be read is narrower than what ``fs_read`` may read: the path must
pass the session's path guard (inside the workspace, symlinks resolved),
no ``ask`` or ``never`` rule over the built-in reads may match it (those
files ask every time, so nothing reads them unasked), and the file must be
UTF-8 text.  Anything else is left out without a word: the summary still
names it, and the agent can ``fs_read`` it.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .context.tokens import TokenCounter
from .provider import ChatMessage
from .tools.boundary import PathGuard, RefusalError

#: The built-in tools whose ``path`` is a file the agent read or changed.
FILE_TOOLS = frozenset({"fs_read", "fs_write", "fs_edit"})
#: Below this many tokens left, a further file is not worth starting.
_MIN_FILE_TOKENS = 64
_CUT_NOTE = "\n[… cut to fit; fs_read the file for the rest]"


@dataclass(frozen=True)
class FileAccess:
    """What the reader may touch: the guard and the read rules that ask."""

    guard: PathGuard
    #: ``args`` globs of every ``ask`` / ``never`` rule over a built-in read.
    read_guards: Sequence[str] = ()


def _paths_in(messages: Sequence[ChatMessage]) -> list[str]:
    """Paths the file tools named in *messages*, newest first, once each."""
    found: list[str] = []
    for message in reversed(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in reversed(message.tool_calls):
            function = call.function
            if function is None or function.name not in FILE_TOOLS:
                continue
            try:
                args = json.loads(function.arguments or "{}")
            except json.JSONDecodeError:
                continue
            path = args.get("path") if isinstance(args, dict) else None
            if isinstance(path, str) and path.strip() and path not in found:
                found.append(path)
    return found


def recent_files(dropped: Sequence[ChatMessage], kept: Sequence[ChatMessage]) -> list[str]:
    """Files read or changed in *dropped*, newest first.

    A file the *kept* turns name too is left out: its read or edit is
    still in the prompt verbatim.
    """
    still_there = set(_paths_in(kept))
    return [path for path in _paths_in(dropped) if path not in still_there]


def _guarded(target: Path, root: Path, globs: Sequence[str], raw: str) -> bool:
    forms = {raw, target.as_posix()}
    if target.is_relative_to(root):
        forms.add(target.relative_to(root).as_posix())
    return any(
        fnmatch.fnmatchcase(form, glob) or fnmatch.fnmatchcase(form.casefold(), glob.casefold())
        for form in forms
        for glob in globs
    )


def _read_text(raw: str, access: FileAccess, max_bytes: int) -> tuple[str, str] | None:
    """(workspace-relative name, text) of *raw*, or None if it may not be read."""
    try:
        target = access.guard.check_read(raw)
    except RefusalError:
        return None
    root_path = access.guard.boundary.workspace_root
    if root_path is None:
        return None
    root = access.guard.canonicalize(root_path)
    if _guarded(target, root, access.read_guards, raw):
        return None
    if not target.is_file():
        return None
    try:
        with target.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        text = data[:max_bytes].decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if "\x00" in text:
        return None
    if len(data) > max_bytes:
        text += _CUT_NOTE
    name = target.relative_to(root).as_posix() if target.is_relative_to(root) else str(target)
    return name, text


def _fit(text: str, tokens: int, room: int) -> str:
    """*text* cut to about *room* tokens, if it is over."""
    if tokens <= room:
        return text
    keep = max(0, int(len(text) * room / tokens) - len(_CUT_NOTE))
    return text[:keep] + _CUT_NOTE


async def read_recent_files(
    paths: Sequence[str],
    access: FileAccess,
    counter: TokenCounter,
    *,
    max_files: int,
    max_tokens: int,
) -> str:
    """The text of up to *max_files* of *paths*, within *max_tokens* tokens.

    Taken in order (newest first); a file over what is left is cut to fit,
    and the rest are left out. Returns "" when nothing fits.
    """
    blocks: list[str] = []
    left = max_tokens
    for raw in paths:
        if len(blocks) >= max_files or left < _MIN_FILE_TOKENS:
            break
        # Four bytes a token is generous for code; the cut below is exact.
        read = await asyncio.to_thread(_read_text, raw, access, left * 4)
        if read is None:
            continue
        name, text = read
        header = f"--- {name} ---\n"
        room = left - counter.count(header).count
        if room < _MIN_FILE_TOKENS:
            break
        body = _fit(text, counter.count(text).count, room)
        block = header + body
        blocks.append(block)
        left -= counter.count(block).count
    return "\n\n".join(blocks)
