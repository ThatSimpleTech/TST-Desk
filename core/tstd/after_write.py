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
  do), for at most its ``timeout_seconds``. It leads its own process
  group, so a timeout or a cancelled turn kills the whole tree (``npx``
  starts ``node`` as a child), as the shell tool does.
- File arguments are workspace-relative against the resolved workspace
  (a symlinked workspace matches the same patterns), and a name that
  starts with ``-`` goes in as ``./-name``, so a file the model named
  can never be read as an option by the user's formatter.
- A failure, a timeout or a command that cannot start goes back to the
  model as a note on the tool result, with the output's start and end. A
  hook that rewrote the file (a formatter) says so, so the model reads it
  again before its next ``fs_edit``. A clean pass that changed nothing adds
  nothing to the model's context.
- Each run is recorded as a :class:`~tstd.protocol.AfterWriteRun` on the
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

from .config import ConfigError
from .hooks_config import AfterWriteHook, HooksConfig
from .logging import get_logger
from .proc_lifecycle import (
    kill_left_leader_running,
    reap_subprocess,
    retain_subprocess_transport,
)
from .protocol import AfterWriteRun

log = get_logger(__name__)

#: The tools whose writes run the hooks.
WRITE_TOOLS = frozenset({"fs_write", "fs_edit"})

#: How much of a failing hook's output goes back to the model.
OUTPUT_CHARS = 2000
_OUTPUT_HEAD = 300

FILES_TOKEN = "{files}"

# After the group kill, how long to wait for the leader's exit.
_REAP_SECS = 2.0


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


def as_argument(rel: str) -> str:
    """*rel* as a file argument no program can read as an option.

    A relative name that starts with ``-`` (the model can create
    ``--config=evil.js``) gets ``./`` in front. ``--`` is not used: not
    every formatter accepts it.
    """
    return f"./{rel}" if rel.startswith("-") else rel


def matches(hook: AfterWriteHook, rel: str) -> bool:
    """Whether *hook*'s patterns pick workspace-relative *rel*."""
    return patterns_match(hook.match, rel)


def patterns_match(patterns: Sequence[str], rel: str) -> bool:
    """A pattern without ``/`` matches the file name, one with ``/`` the path."""
    name = PurePosixPath(rel).name
    for pattern in patterns:
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


async def _communicate(proc: asyncio.subprocess.Process, timeout: float) -> bytes:  # noqa: ASYNC109
    """The hook's output. On timeout or cancel, its whole tree is killed first.

    ``proc.kill()`` alone would reach only the leader: ``npx`` runs
    ``node`` as a child, which would keep rewriting the file after the
    diff was taken and hold the pipe open for the reap's full grace.
    """
    from .tools.shell import _kill_process_group, _kill_process_group_offloaded

    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out
    except TimeoutError:
        note = await _kill_process_group_offloaded(proc)
        await _reap(proc, note)
        raise
    except asyncio.CancelledError:
        # Sync on purpose: an await here could be cancelled again before
        # the kill lands.
        note = _kill_process_group(proc)
        await _reap(proc, note)
        raise
    finally:
        retain_subprocess_transport(proc)


async def _reap(proc: asyncio.subprocess.Process, note: str | None) -> None:
    if note:
        log.warning("after-write hook kill: %s (pid %s)", note, proc.pid)
    if not kill_left_leader_running(proc, note):
        await reap_subprocess(proc, timeout=_REAP_SECS)


async def _run_one(
    hook: AfterWriteHook, files: list[Path], cwd: Path, env: dict[str, str], root: Path | None
) -> tuple[AfterWriteRun, str]:
    """Run *hook* on *files*. ``(the record, the note for the model or "")``."""
    label = hook.label
    shown = [as_argument(_relative(f, root)) for f in files]
    argv = build_argv(hook, shown)
    program = shutil.which(argv[0], path=env.get("PATH"))
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    if program is None:
        run = AfterWriteRun(name=label, status="error", duration_ms=elapsed_ms())
        return run, f"[after-write hook `{label}` could not start: {argv[0]} is not on PATH]"
    before = {f: await asyncio.to_thread(_digest, f) for f in files}
    from .tools.shell import _CREATE_NEW_PROCESS_GROUP, _CREATE_NO_WINDOW

    try:
        proc = await asyncio.create_subprocess_exec(
            program,
            *argv[1:],
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            # Its own group, so a stop kills grandchildren too. The
            # flags are 0 off Windows, where ``start_new_session`` is ignored.
            start_new_session=True,
            creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
        )
    except OSError as e:
        run = AfterWriteRun(name=label, status="error", duration_ms=elapsed_ms())
        return run, f"[after-write hook `{label}` could not start: {e}]"
    try:
        out = await _communicate(proc, hook.timeout_seconds)
    except TimeoutError:
        run = AfterWriteRun(name=label, status="timed_out", duration_ms=elapsed_ms())
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
        run = AfterWriteRun(
            name=label, status="passed", exit_code=0, duration_ms=ms, changed=bool(changed_files)
        )
    else:
        run = AfterWriteRun(
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
) -> tuple[tuple[AfterWriteRun, ...], str]:
    """Run every configured hook that matches *files*, in order.

    Returns:
        The runs, for the ``tool_result`` event, and the text to append
        to the model's tool result (empty when there is nothing to say).
        Never raises: a hook that cannot be judged is an ``error`` run.
    """
    if not files:
        return (), ""
    # An autonomous session's commands run only in its container (TD-5134
    # puts its hooks there too). After-write hooks have no container path
    # yet, so they do not run at all rather than run on the host.
    if getattr(session, "autonomy", False):
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
    # The written paths are canonical (PathGuard resolves symlinks), so the
    # root must be too, or a symlinked workspace (macOS /tmp, a linked
    # ~/code) matches no path pattern and hands out absolute paths.
    root = (
        await asyncio.to_thread(Path(raw_root).resolve)
        if isinstance(raw_root, str) and raw_root
        else None
    )
    cwd = root if root is not None and root.is_dir() else Path(files[0]).parent
    env = sanitized_env()
    runs: list[AfterWriteRun] = []
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
                AfterWriteRun(name=hook.label, status="error"),
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
