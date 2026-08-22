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
from ..compaction import budget_threshold, estimate_tokens
from ..config import ModelConfig, TierName
from ..context.manifest import _FALLBACK_IGNORE
from ..context.skills import (
    FALLBACK_SKILL_BUDGET_TOKENS,
    LoadedSkill,
    discover_skills,
    read_skill_body,
    render_loaded_skill,
)
from ..context.tokens import HeuristicTokenCounter
from ..desktop import DesktopDriver, MockDesktopDriver
from .browser import register_browser_handlers
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


async def load_skill(
    session: object, name: str, tool_call_id: str = "", *, model_config: ModelConfig | None = None
) -> str:
    """Load a skill body by name, whole (TD-4502).

    The catalog told the brain the skill exists; this delivers the prose.
    An oversized body is refused with its size stated — never truncated,
    because half a skill reads as a whole one. Successful loads are
    recorded on the session so the inspector lists them apart from
    steering.
    """
    workspace = getattr(session, "workspace_path", None)
    if not workspace:
        return "Error: load_skill needs an open workspace."
    skills = await asyncio.to_thread(discover_skills, Path(workspace))
    skill = next((s for s in skills if s.name == name), None)
    if skill is None:
        available = ", ".join(s.name for s in skills[:10]) or "(none)"
        return f"Error: no skill named {name!r}. Available: {available}"
    try:
        body = await asyncio.to_thread(read_skill_body, skill.path)
    except (OSError, UnicodeDecodeError) as exc:
        return f"Error: skill {name!r} is unreadable ({exc})."

    tokens = HeuristicTokenCounter().count(body).count
    remaining = _remaining_skill_budget(session, model_config)
    if tokens > remaining:
        return (
            f"Error: skill {name!r} is about {tokens} tokens but only {remaining} "
            f"remain in the context budget; refused rather than truncated. "
            f"Free up context (finish or compact turns) and try again."
        )
    loaded_skills = getattr(session, "loaded_skills", None)
    if isinstance(loaded_skills, dict):
        loaded_skills[name] = LoadedSkill(
            name=skill.name, source=skill.source, path=str(skill.path), tokens=tokens
        )
    return render_loaded_skill(skill, body)


def _remaining_skill_budget(session: object, model_config: ModelConfig | None) -> int:
    """Compaction-budget headroom for one more injected block.

    The active tier's compaction threshold minus what the conversation
    already estimates — the same arithmetic that gates a send, applied to
    a single load so a skill cannot walk the window into compaction on
    its own. Without a wired config (tests), a conservative floor holds.
    """
    tier_name: TierName = "brain"
    router = getattr(session, "router", None)
    if router is not None:
        tier_name = router.active_tier
    if model_config is None:
        return FALLBACK_SKILL_BUDGET_TOKENS
    try:
        threshold = budget_threshold(model_config.tier(tier_name))
    except KeyError:
        return FALLBACK_SKILL_BUDGET_TOKENS
    conversation = list(getattr(session, "conversation", []) or [])
    used, _method = estimate_tokens(conversation, HeuristicTokenCounter())
    return max(0, threshold - used)


def register_builtin_handlers(
    dispatcher: ToolDispatcher,
    allowed_commands: tuple[str, ...] | None = None,
    desktop_driver: DesktopDriver | None = None,
    browser_driver: BrowserDriver | None = None,
    model_config: ModelConfig | None = None,
) -> None:
    """Register the built-in tool handlers on *dispatcher*.

    ``allowed_commands`` restricts the shell tool to the given binaries
    when set (TD-605); ``None`` leaves it unrestricted.  ``desktop_driver``
    is the process-wide computer-use backend (TD-3301); omitted means the
    in-process mock so every builtin still has a handler.  ``browser_driver``
    is the session browser (TD-1710); omitted is the in-process mock.
    ``model_config`` sizes the load_skill budget from the active tier's
    context window (TD-4502); omitted falls back to a fixed floor.
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
    dispatcher.register_handler("load_skill", partial(load_skill, model_config=model_config))
    register_desktop_handlers(
        dispatcher, desktop_driver if desktop_driver is not None else MockDesktopDriver()
    )
    register_browser_handlers(
        dispatcher, browser_driver if browser_driver is not None else MockBrowserDriver()
    )
