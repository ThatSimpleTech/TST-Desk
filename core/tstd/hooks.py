"""Hook runner (TD-5134).

A hook is a command from the user's ``config.yaml`` or, once trusted
(TD-5113), the workspace's ``.tst/config.yaml``. It runs at one of five
points: ``session_start``, ``user_prompt``, ``before_tool``,
``after_tool`` and ``stop``. It gets the event as JSON on stdin and
answers with its exit code or a JSON object on stdout:

- exit 0 and nothing printed: nothing to say.
- exit 0 and a JSON object: ``decision`` (``block``, ``ask``, ``allow``),
  ``reason`` and ``context``. ``context`` is text for the model.
- exit 0 and plain text: context for ``session_start`` and
  ``user_prompt``; ignored for the others, as in Claude Code.
- exit 2: block, with stderr as the reason.
- any other exit, a timeout, or a command that will not start: recorded
  and otherwise ignored.

Hooks only ever tighten. ``allow`` is ignored. A ``before_tool`` hook runs
after the decision classifier and the policy have decided (directive 6),
and only for a call they would let run: ``block`` refuses it and ``ask``
turns an automatic run into an approval. Nothing a hook says can make a
call run that would not have, or skip the approval it would have needed.

Every run is a ``hook_run`` event on the owning session's log, which the
audit writer stores (redacted, like every event), and the runner keeps
the last run of each hook for the inspector (``get_hooks``).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import fnmatch
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .hooks_config import (
    HOOK_EVENTS,
    TOOL_EVENTS,
    HookEntry,
    HookEvent,
    HooksConfig,
    HooksConfigError,
    hooks_from_mapping,
)
from .logging import get_logger
from .protocol import HookInfo, HookResult, HookRun, HooksState

if TYPE_CHECKING:
    from .session import Session

log = get_logger(__name__)

HookSource = Literal["user", "workspace"]

#: Exit code that blocks, as in Claude Code.
BLOCK_EXIT_CODE = 2
#: How much a hook may print that TST Desk reads; the rest is dropped.
MAX_READ_BYTES = 64_000
#: How much of a hook's text goes to the model, the window and the audit.
MAX_TEXT_CHARS = 2000
#: How many times a ``stop`` hook may send the model back in one turn.
MAX_STOP_BLOCKS = 3


@dataclass(frozen=True)
class ActiveHook:
    """One configured hook, where it came from and its place in the list."""

    source: HookSource
    event: HookEvent
    index: int
    entry: HookEntry

    @property
    def id(self) -> str:
        return f"{self.source}:{self.event}:{self.index}"

    def matches(self, tool_name: str | None) -> bool:
        """True when this hook runs for *tool_name* (always, off tool events)."""
        if self.event not in TOOL_EVENTS or not self.entry.tools:
            return True
        return tool_name is not None and any(
            fnmatch.fnmatchcase(tool_name, pattern) for pattern in self.entry.tools
        )


@dataclass(frozen=True)
class HookOutcome:
    """What one run said, before it is turned into an event."""

    result: HookResult
    exit_code: int | None = None
    reason: str = ""
    context: str = ""
    output: str = ""
    duration_ms: int = 0


@dataclass
class HookVerdict:
    """Every matching hook's answer for one event, folded."""

    block: str | None = None
    ask: str | None = None
    contexts: list[str] = field(default_factory=list)

    @property
    def context(self) -> str:
        return "\n\n".join(c for c in self.contexts if c)


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def interpret(event: HookEvent, exit_code: int, stdout: str, stderr: str) -> HookOutcome:
    """Turn one finished run into an outcome (no timing yet)."""
    output = _clip("\n".join(part for part in (stdout.strip(), stderr.strip()) if part))
    if exit_code == BLOCK_EXIT_CODE:
        reason = _clip(stderr) or _clip(stdout) or "a hook blocked this"
        return HookOutcome("block", exit_code, reason=reason, output=output)
    if exit_code != 0:
        return HookOutcome("error", exit_code, reason=f"exited {exit_code}", output=output)
    text = stdout.strip()
    if not text:
        return HookOutcome("ok", 0, output=output)
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        if event in ("session_start", "user_prompt"):
            return HookOutcome("context", 0, context=_clip(text), output=output)
        return HookOutcome("ok", 0, output=output)
    return _from_json(event, data, output)


def _from_json(event: HookEvent, data: dict[str, Any], output: str) -> HookOutcome:
    decision = data.get("decision")
    reason = data.get("reason")
    context = data.get("context")
    if decision not in (None, "block", "ask", "allow") or not isinstance(
        reason if reason is not None else "", str
    ):
        return HookOutcome("error", 0, reason="unreadable JSON answer", output=output)
    if context is not None and not isinstance(context, str):
        return HookOutcome("error", 0, reason="unreadable JSON answer", output=output)
    reason_text = _clip(reason or "")
    context_text = _clip(context or "")
    if decision == "block":
        return HookOutcome(
            "block",
            0,
            reason=reason_text or "a hook blocked this",
            context=context_text,
            output=output,
        )
    # ``ask`` means something only before a tool runs; ``allow`` never
    # loosens anything, so both read as no decision elsewhere.
    if decision == "ask" and event == "before_tool":
        return HookOutcome(
            "ask",
            0,
            reason=reason_text or "a hook asked for approval",
            context=context_text,
            output=output,
        )
    if context_text:
        return HookOutcome("context", 0, context=context_text, output=output)
    return HookOutcome("ok", 0, output=output)


async def _read_capped(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    data = await stream.read(MAX_READ_BYTES)
    # Drain the rest so the hook does not block on a full pipe.
    while await stream.read(65536):
        pass
    return data


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
            return
        # TD-5161: the tree, as the shell tool does; proc.kill() reached
        # only the shell, and what it started kept the pipes open.
        from .tools.shell import _kill_windows_tree

        if _kill_windows_tree(proc.pid) is not None:
            proc.kill()


async def run_command(
    hook: ActiveHook,
    payload: dict[str, Any],
    cwd: str,
    env: dict[str, str],
    *,
    argv: list[str] | None = None,
) -> HookOutcome:
    """Run *hook* once with *payload* on stdin and judge what it said.

    *argv*, when given, is what runs instead of the host shell: an
    autonomous session's container (:func:`sandboxed_argv`).
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    extra: dict[str, Any] = {}
    if sys.platform != "win32":
        # Its own process group, so a timeout kills what it started too.
        extra["start_new_session"] = True
    else:
        # A group leader, so taskkill /T walks only its tree, and no console
        # window flashes from the windowless daemon (TD-5161).
        from .tools.shell import _CREATE_NEW_PROCESS_GROUP, _CREATE_NO_WINDOW

        extra["creationflags"] = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    pipes: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": cwd,
        "env": env,
        **extra,
    }
    try:
        if argv is not None:
            proc = await asyncio.create_subprocess_exec(*argv, **pipes)
        else:
            proc = await asyncio.create_subprocess_shell(hook.entry.command, **pipes)
    except OSError as e:
        return HookOutcome("error", None, reason=f"could not start: {e}", duration_ms=elapsed())

    async def converse() -> tuple[bytes, bytes, int]:
        if proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(json.dumps(payload).encode("utf-8"))
                await proc.stdin.drain()
            proc.stdin.close()
        out, err = await asyncio.gather(_read_capped(proc.stdout), _read_capped(proc.stderr))
        return out, err, await proc.wait()

    try:
        out, err, code = await asyncio.wait_for(converse(), timeout=hook.entry.timeout_seconds)
    except TimeoutError:
        _kill(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)
        return HookOutcome(
            "timeout",
            None,
            reason=f"killed after {hook.entry.timeout_seconds:g}s",
            duration_ms=elapsed(),
        )
    except asyncio.CancelledError:
        _kill(proc)
        raise
    outcome = interpret(
        hook.event,
        code,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )
    return HookOutcome(
        outcome.result,
        outcome.exit_code,
        outcome.reason,
        outcome.context,
        outcome.output,
        elapsed(),
    )


class HookRunner:
    """A session's hooks: what is active, running them, and their last results.

    *owner* is the session whose log the runs go to. A delegate child
    shares its parent's runner, so its tool calls meet the same hooks and
    their runs land on the parent's audited log.
    """

    def __init__(self, owner: Session) -> None:
        self.owner = owner
        self.user = HooksConfig()
        self.last: dict[str, HookRun] = {}
        # session_start context waiting for the next user turn.
        self.pending_context: list[str] = []
        self.stop_blocks = 0
        # TD-5161: a delegate child in its own worktree runs the shared
        # hooks there, so an after_tool lint checks the child's edit.
        self.cwd: str | None = None

    def in_folder(self, cwd: str) -> HookRunner:
        """The same hooks, results and log, run in *cwd* (a child's worktree)."""
        if cwd == (self.cwd or self.owner.workspace_path):
            return self
        moved = copy.copy(self)
        moved.cwd = cwd
        return moved

    # ── What is active ────────────────────────────────────────────────

    def workspace(self) -> tuple[HooksConfig, str | None]:
        """The workspace's hooks while its config applies, and any error."""
        view = self.owner.config_view
        if view is None or not view.applies:
            return HooksConfig(), None
        try:
            return hooks_from_mapping(view.path, view.effective), None
        except HooksConfigError as e:
            return HooksConfig(), str(e)

    def active(self, event: HookEvent | None = None) -> list[ActiveHook]:
        """User hooks, then workspace hooks, for *event* (or all)."""
        workspace, _ = self.workspace()
        found: list[ActiveHook] = []
        for name in HOOK_EVENTS if event is None else (event,):
            for source, cfg in (("user", self.user), ("workspace", workspace)):
                for i, entry in enumerate(cfg.entries(name)):
                    found.append(ActiveHook(source, name, i, entry))  # type: ignore[arg-type]
        return found

    def state(self) -> HooksState:
        """The inspector's view: every active hook and its last run."""
        workspace_error = self.workspace()[1]
        view = self.owner.config_view
        return HooksState(
            session_id=self.owner.id,
            hooks=[
                HookInfo(
                    id=hook.id,
                    source=hook.source,
                    event=hook.event,
                    command=hook.entry.command,
                    name=hook.entry.name,
                    timeout_seconds=hook.entry.timeout_seconds,
                    tools=list(hook.entry.tools),
                    last=self.last.get(hook.id),
                )
                for hook in self.active()
            ],
            workspace_state=view.state if view is not None else "none",
            workspace_error=workspace_error,
            seq=1,
        )

    # ── Running ───────────────────────────────────────────────────────

    async def run(
        self,
        event: HookEvent,
        payload: dict[str, Any],
        *,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> HookVerdict:
        """Run every matching hook for *event*, in order, and fold the answers.

        A block stops the rest from running: the thing is refused anyway.
        """
        verdict = HookVerdict()
        hooks = [hook for hook in self.active(event) if hook.matches(tool_name)]
        if not hooks:
            return verdict
        session = self.owner
        # An autonomous session's hooks run in its container, as its shell
        # calls do (§12.5, TD-4301): the agent can write the workspace
        # unattended, so a hook that runs a repo script must not hand it
        # the host. There the workspace is ``/workspace``.
        from .autonomy.sandbox import WORKSPACE_DEST

        sandboxed = bool(getattr(session, "autonomy", False))
        folder = self.cwd or session.workspace_path
        workspace = WORKSPACE_DEST if sandboxed else folder
        body = {
            "event": event,
            "session_id": session.id,
            "workspace": workspace,
            **payload,
        }
        hook_env = {
            "TST_HOOK_EVENT": event,
            "TST_SESSION_ID": session.id,
            "TST_WORKSPACE": workspace,
        }
        # Lazy: ``tools`` imports ``session``, which imports this module.
        from .tools.shell import sanitized_env

        env = sanitized_env()
        if not sandboxed:
            env.update(hook_env)
        for hook in hooks:
            argv: list[str] | None = None
            if sandboxed:
                try:
                    argv = await sandboxed_argv(session, hook.entry.command, hook_env)
                except ValueError as e:
                    outcome = HookOutcome("error", None, reason=f"could not start: {e}")
                    await self._record(hook, outcome, tool_name, tool_call_id)
                    continue
            outcome = await run_command(hook, body, folder, env, argv=argv)
            await self._record(hook, outcome, tool_name, tool_call_id)
            if outcome.context:
                verdict.contexts.append(outcome.context)
            if outcome.result == "ask" and verdict.ask is None:
                verdict.ask = outcome.reason
            if outcome.result == "block":
                verdict.block = outcome.reason
                break
        return verdict

    async def _record(
        self,
        hook: ActiveHook,
        outcome: HookOutcome,
        tool_name: str | None,
        tool_call_id: str | None,
    ) -> None:
        run = HookRun(
            session_id=self.owner.id,
            hook_id=hook.id,
            source=hook.source,
            event=hook.event,
            command=hook.entry.command,
            result=outcome.result,
            exit_code=outcome.exit_code,
            reason=outcome.reason,
            context=outcome.context,
            output=outcome.output,
            duration_ms=outcome.duration_ms,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            seq=1,
        )
        # The event log redacts on the way in (TD-1405); keep what it kept.
        stored = await self.owner.event_log.add(run)
        self.last[hook.id] = stored if isinstance(stored, HookRun) else run
        log.info(
            "hook ran",
            extra={
                "extra_fields": {
                    "session_id": self.owner.id,
                    "hook_id": hook.id,
                    "result": outcome.result,
                    "duration_ms": outcome.duration_ms,
                }
            },
        )


async def sandboxed_argv(session: Any, command: str, env: dict[str, str]) -> list[str]:
    """The container argv an autonomous session's hook runs under.

    The same container its shell calls get: only the workspace bound in,
    the charter's network, and the session's memory cap, with stdin kept
    open for the payload. Raises ``ValueError`` when it cannot be built.
    """
    from .autonomy.sandbox import SandboxError, autonomy_shell_argv
    from .tools import shell_memory
    from .tools.shell import sanitized_env

    cfg = session.config
    if cfg is None:
        raise ValueError("an autonomous hook needs the session's model config")
    network: str | list[str] = "deny"
    if session.charter is not None:
        network = session.charter.boundary.network
    cap = await shell_memory.plan_cap(session, sanitized_env())
    try:
        return autonomy_shell_argv(
            Path(session.workspace_path),
            command,
            runtime=cfg.autonomy.runtime,
            image=cfg.autonomy.image,
            network=network,
            memory_mb=cap.mb if cap is not None else None,
            interactive=True,
            env=env,
        )
    except SandboxError as e:
        raise ValueError(str(e)) from e


def hook_runner(session: Any) -> HookRunner | None:
    """The runner a session's calls meet, or None (library use, tests)."""
    runner = getattr(session, "hooks", None)
    return runner if isinstance(runner, HookRunner) else None


#: How much of a tool's result an ``after_tool`` hook is sent.
MAX_PAYLOAD_OUTPUT_CHARS = 20_000


async def before_tool(
    runner: HookRunner,
    tool_call_id: str,
    name: str,
    arguments: dict[str, Any],
    decision_class: Any,
    effect: str,
) -> HookVerdict:
    """Run ``before_tool`` hooks for a call the policy would let run (or ask)."""
    klass = getattr(decision_class, "value", decision_class)
    return await runner.run(
        "before_tool",
        {
            "tool": {
                "name": name,
                "call_id": tool_call_id,
                "arguments": arguments,
                "decision_class": klass,
                "effect": effect,
            }
        },
        tool_name=name,
        tool_call_id=tool_call_id,
    )


async def after_tool(
    runner: HookRunner,
    tool_call_id: str,
    name: str,
    arguments: dict[str, Any],
    output: str,
    before_context: str,
) -> str:
    """What ``before_tool`` and ``after_tool`` hooks said, to add after *output*.

    Empty when they said nothing. The caller adds it after truncating the
    result, so a long result cannot cut the hooks' notes off.
    """
    verdict = await runner.run(
        "after_tool",
        {
            "tool": {"name": name, "call_id": tool_call_id, "arguments": arguments},
            "result": {"status": "success", "output": output[:MAX_PAYLOAD_OUTPUT_CHARS]},
        },
        tool_name=name,
        tool_call_id=tool_call_id,
    )
    notes = [text for text in (before_context, verdict.context) if text]
    if verdict.block is not None:
        notes.append(f"A hook flagged this result: {verdict.block}")
    if not notes:
        return ""
    return "\n\n[hooks]\n" + "\n\n".join(notes)


# Opens the note below. Restored history cuts a user message there (TD-5201).
HOOK_NOTE_HEADER = "[Context from hooks]"


def with_hook_context(content: Any, text: str) -> Any:
    """A user message's *content* with hook context after the person's words."""
    if not text:
        return content
    note = f"{HOOK_NOTE_HEADER}\n{text}"
    if isinstance(content, str):
        return f"{content}\n\n{note}"
    return [*content, {"type": "text", "text": note}]
