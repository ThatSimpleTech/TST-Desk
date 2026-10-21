"""Run a formatter or linter after the agent writes a file (TD-5135).

Local models make more format and lint slips than hosted ones. An
``after_write`` hook runs a configured command on the files an ``fs_write``
or ``fs_edit`` just changed, so a slip goes back to the model in the same
tool result instead of costing a turn.

- Hooks come from the user's ``config.yaml`` (``hooks.after_write``) and
  from the workspace's ``.tst/config.yaml``, the latter only once that
  file is in force (TD-5113): a repo's command never runs untrusted.
- The command runs without a shell, in the workspace, with secrets
  stripped from its environment (as the shell tool and ``worktree.setup``
  do), for at most its ``timeout_seconds``.
- A failure, a timeout or a command that cannot start goes back to the
  model as a note on the tool result, with the output's start and end. A
  hook that rewrote the file (a formatter) says so, so the model reads it
  again before its next ``fs_edit``. A clean pass that changed nothing adds
  nothing to the model's context.
- Each run is recorded as a :class:`~tstd.protocol.HookRun` on the
  ``tool_result`` event, with its time, for the step timeline.

The hook is the user's own configured command, not a tool call the model
made, so it does not go through the decision classifier, like
``worktree.setup``. It cannot widen anything the model may do: it only
ever sees files a classified, approved write already changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from .config import AfterWriteHook, ConfigError, HooksConfig
from .logging import get_logger
from .proc_lifecycle import finish_subprocess
from .protocol import HookRun

log = get_logger(__name__)

#: The tools whose writes run the hooks.
WRITE_TOOLS = frozenset({"fs_write", "fs_edit"})

#: How much of a failing hook's output goes back to the model.
OUTPUT_CHARS = 2000
_OUTPUT_HEAD = 300

FILES_TOKEN = "{files}"


def clip_output(text: str, limit: int = OUTPUT_CHARS) -> str:
    """The start and end of *text*, at most about *limit* characters."""
    text = text.strip()
    if len(text) <= limit:
        return text
    tail = limit - _OUTPUT_HEAD
    cut = len(text) - _OUTPUT_HEAD - tail
    return f"{text[:_OUTPUT_HEAD]}\n[... {cut} characters cut ...]\n{text[-tail:]}"


def workspace_hooks(data: dict[str, Any] | None) -> list[AfterWriteHook]:
    """``hooks.after_write`` from a workspace config mapping already in force.

    A section that does not validate applies nothing; it never takes the
    rest of the file down with it.
    """
    if not isinstance(data, dict) or data.get("hooks") is None:
        return []
    try:
        return list(HooksConfig.model_validate(data["hooks"]).after_write)
    except ValidationError as e:
        log.warning(
            "workspace hooks section invalid; no workspace hook runs",
            extra={"extra_fields": {"error": str(e.errors()[0].get("msg", ""))}},
        )
        return []


def session_hooks(session: object) -> list[AfterWriteHook]:
    """The user's hooks, then the workspace's when its config is in force."""
    hooks: list[AfterWriteHook] = []
    config = getattr(session, "config", None)
    user = getattr(getattr(config, "hooks", None), "after_write", None)
    if isinstance(user, list):
        hooks.extend(h for h in user if isinstance(h, AfterWriteHook))
    workspace = getattr(session, "workspace_path", None)
    if isinstance(workspace, str) and workspace:
        from .workspace_trust import read_view

        try:
            view = read_view(workspace, getattr(session, "trust_store", None))
        except ConfigError:
            return hooks
        hooks.extend(workspace_hooks(view.effective))
    return hooks


def _relative(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def matches(hook: AfterWriteHook, rel: str) -> bool:
    """A pattern without ``/`` matches the file name, one with ``/`` the path."""
    name = PurePosixPath(rel).name
    for pattern in hook.match:
        target = rel if "/" in pattern else name
        if fnmatchcase(target, pattern.lstrip("/")):
            return True
    return False


def build_argv(hook: AfterWriteHook, files: Sequence[str]) -> list[str]:
    """The command with ``{files}`` replaced by *files*, or *files* appended."""
    if FILES_TOKEN not in hook.command:
        return [*hook.command, *files]
    argv: list[str] = []
    for word in hook.command:
        if word == FILES_TOKEN:
            argv.extend(files)
        else:
            argv.append(word)
    return argv


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


async def _run_one(
    hook: AfterWriteHook, files: list[Path], cwd: Path, env: dict[str, str], root: Path | None
) -> tuple[HookRun, str]:
    """Run *hook* on *files*. ``(the record, the note for the model or "")``."""
    label = hook.label
    shown = [_relative(f, root) for f in files]
    argv = build_argv(hook, shown)
    program = shutil.which(argv[0], path=env.get("PATH"))
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    if program is None:
        run = HookRun(name=label, status="error", duration_ms=elapsed_ms())
        return run, f"[after-write hook `{label}` could not start: {argv[0]} is not on PATH]"
    before = {f: await asyncio.to_thread(_digest, f) for f in files}
    try:
        proc = await asyncio.create_subprocess_exec(
            program,
            *argv[1:],
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as e:
        run = HookRun(name=label, status="error", duration_ms=elapsed_ms())
        return run, f"[after-write hook `{label}` could not start: {e}]"
    try:
        out, _err = await finish_subprocess(proc, timeout=hook.timeout_seconds)
    except TimeoutError:
        run = HookRun(name=label, status="timed_out", duration_ms=elapsed_ms())
        return run, (
            f"[after-write hook `{label}` stopped after {hook.timeout_seconds:g} s "
            "without finishing]"
        )
    ms = elapsed_ms()
    changed_files = [f for f in files if await asyncio.to_thread(_digest, f) != before[f]]
    code = proc.returncode
    seconds = f"{ms / 1000:.2f} s"
    notes: list[str] = []
    if code == 0:
        run = HookRun(
            name=label, status="passed", exit_code=0, duration_ms=ms, changed=bool(changed_files)
        )
    else:
        run = HookRun(
            name=label, status="failed", exit_code=code, duration_ms=ms, changed=bool(changed_files)
        )
        text = clip_output(out.decode("utf-8", "replace"))
        notes.append(
            f"[after-write hook `{label}` failed: exit {code} after {seconds}. "
            "Fix what it reports.]" + (f"\n{text}" if text else "")
        )
    if changed_files:
        names = ", ".join(_relative(f, root) for f in changed_files)
        notes.append(
            f"[after-write hook `{label}` changed {names}; read it again before editing it]"
        )
    return run, "\n".join(notes)


async def run_after_write(
    session: object, files: Sequence[Path]
) -> tuple[tuple[HookRun, ...], str]:
    """Run every configured hook that matches *files*, in order.

    Returns:
        The runs, for the ``tool_result`` event, and the text to append
        to the model's tool result (empty when there is nothing to say).
        Never raises: a hook that cannot be judged is an ``error`` run.
    """
    if not files:
        return (), ""
    try:
        hooks = await asyncio.to_thread(session_hooks, session)
    except Exception:
        log.exception("after-write hooks could not be read")
        return (), ""
    if not hooks:
        return (), ""
    from .tools.shell import sanitized_env

    raw_root = getattr(session, "workspace_path", None)
    root = Path(raw_root) if isinstance(raw_root, str) and raw_root else None
    cwd = root if root is not None and root.is_dir() else Path(files[0]).parent
    env = sanitized_env()
    runs: list[HookRun] = []
    notes: list[str] = []
    for hook in hooks:
        chosen = [Path(f) for f in files if matches(hook, _relative(Path(f), root))]
        if not chosen:
            continue
        try:
            run, note = await _run_one(hook, chosen, cwd, env, root)
        except Exception as e:
            log.exception("after-write hook raised", extra={"extra_fields": {"hook": hook.label}})
            run, note = (
                HookRun(name=hook.label, status="error"),
                f"[after-write hook `{hook.label}` could not run: {e}]",
            )
        log.info(
            "after-write hook ran",
            extra={
                "extra_fields": {
                    "hook": run.name,
                    "status": run.status,
                    "exit_code": run.exit_code,
                    "duration_ms": run.duration_ms,
                }
            },
        )
        runs.append(run)
        if note:
            notes.append(note)
    return tuple(runs), "\n".join(notes)
