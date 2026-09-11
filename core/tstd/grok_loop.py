"""ACP-backed session loop — drop-in replacement for ``agent_loop``.

When ``engine.kind`` is ``grok``, the daemon starts this loop instead of
the native 3-tier OpenAI-compatible loop. The window, approvals, coworker
mode, and event log stay TST Desk's. The agent is the installed ``grok``
binary, spoken to over ACP.

CLI capabilities are not replaced: ``grok``, ``grok -p``, and
``grok agent stdio`` keep working independently. This loop is one more
ACP client, like Zed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .grok_acp import (
    AcpClient,
    GrokEngineError,
    find_grok_binary,
    persist_prompt_images,
    pick_permission_option,
    prompt_image_supported,
)
from .grok_home import acp_mcp_servers, media_kind, resolve_workspace_file, sniff_local_url
from .logging import get_logger
from .protocol import (
    AssistantDelta,
    AssistantReasoning,
    CostUpdate,
    Error,
    GrokCommand,
    GrokCommands,
    GrokMode,
    GrokPlan,
    GrokPlanEntry,
    GrokPreview,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from .provider import ChatMessage
from .session import Session

log = get_logger("tstd.grok_loop")

# After session/cancel, wait this long for Grok to finish the in-flight
# session/prompt. Computer-use MCP often ignores cancel until a tool
# returns; past this we kill the agent so the next message can run.
PROMPT_CANCEL_GRACE_S = 1.5


def grok_argv(binary: Path, *, yolo: bool) -> list[str]:
    """Build the ``grok agent stdio`` argv.

    ``--no-leader`` keeps tools in-process so a sandbox profile (if the
    user set one in Grok config) is not refused. ``--always-approve`` is
    only added when TST Desk skip-all is on — Grok's own deny rules still
    apply.
    """
    argv = [str(binary), "agent"]
    if yolo:
        argv.append("--always-approve")
    argv.extend(["--no-leader", "stdio"])
    return argv


def grok_env() -> dict[str, str]:
    """Environment for the agent process.

    ``TERM_PROGRAM=Grok Desktop`` is the slot the CLI already detects for
    native notifications. ``GROK_ACP_CLIENT`` names the wrapper.
    """
    env = os.environ.copy()
    env.setdefault("TERM_PROGRAM", "Grok Desktop")
    env["GROK_ACP_CLIENT"] = "tst-desk"
    env.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
    from .cu_host import SOCK_ENV

    sock = os.environ.get(SOCK_ENV, "").strip()
    if sock:
        env[SOCK_ENV] = sock
    return env


def _session_id_path(session: Session) -> Path | None:
    persist_dir = getattr(session, "persist_dir", None)
    if persist_dir is None:
        return None
    return Path(persist_dir) / "grok_session_id"


def _load_grok_session_id(session: Session) -> str | None:
    path = _session_id_path(session)
    if path is None or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _save_grok_session_id(session: Session, grok_id: str) -> None:
    path = _session_id_path(session)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(grok_id + "\n", encoding="utf-8")


def grok_model_from_payload(payload: dict[str, Any] | None) -> str | None:
    """Pull a model id out of an ACP result or session update.

    Grok puts ``modelId`` on ``_meta`` of session updates; some payloads
    name it at the top level. Empty / missing stays None — the greeting
    must not invent a slug the CLI did not name.
    """
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = []
    meta = payload.get("_meta")
    if isinstance(meta, dict):
        candidates.extend(meta.get(key) for key in ("modelId", "model_id", "model"))
    update = payload.get("update")
    if isinstance(update, dict):
        nested = grok_model_from_payload(update)
        if nested:
            return nested
    candidates.extend(payload.get(key) for key in ("modelId", "model_id", "currentModelId"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _block_text(block: dict[str, Any]) -> str:
    """Visible text from one ACP content block. Non-text blocks stay out of chat."""
    kind = str(block.get("type") or "text")
    if kind != "text":
        return ""
    text = block.get("text")
    return text if isinstance(text, str) else ""


def _update_text(update: dict[str, Any]) -> str:
    content = update.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _block_text(content)
    if isinstance(content, list):
        return "".join(_block_text(block) for block in content if isinstance(block, dict))
    return ""


def _is_leaked_tool_json(text: str) -> bool:
    """True when a chunk is tool-argument JSON, not prose for the user."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped in {"}", "]", "},", "],"}:
        return True
    if stripped.startswith('"') and '":' in stripped[:48]:
        return True
    return stripped.startswith("{") and any(
        key in stripped[:120] for key in ('"is_background"', '"command"', '"path"', '"target_file"')
    )


def _visible_delta(previous: str, incoming: str, *, after_tool: bool) -> str:
    """The assistant_delta to emit for this chunk. Empty means drop it.

    Grok often starts a new sentence after a tool without a leading space
    or a newline. Token-level chunks still join unchanged.
    """
    if _is_leaked_tool_json(incoming):
        return ""
    chunk = incoming
    if after_tool and chunk and not chunk.startswith("\n"):
        return "\n\n" + chunk
    if not previous or not chunk:
        return chunk
    if previous[-1].isspace() or chunk[0].isspace():
        return chunk
    if previous[-1] in ".!?:;" and (chunk[0].isupper() or chunk[0] in "\"'"):
        return " " + chunk
    return chunk


def _tool_name(update: dict[str, Any]) -> str:
    """The tool that ran. Grok Build wraps MCP calls in ``use_tool`` with
    ``{"tool_name", "tool_input"}``; unwrap so the timeline, approvals, and
    the computer-use episode see ``computer-use__click``, not the wrapper."""
    inner = _raw_input(update).get("tool_name")
    if isinstance(inner, str) and inner:
        return inner
    for key in ("title", "toolName", "kind", "name"):
        value = update.get(key)
        if isinstance(value, str) and value:
            return value
    return "tool"


def _raw_input(update: dict[str, Any]) -> dict[str, Any]:
    raw = update.get("rawInput") or update.get("raw_input") or update.get("input")
    return raw if isinstance(raw, dict) else {}


def _tool_input(update: dict[str, Any]) -> dict[str, Any]:
    """The tool's arguments; a ``use_tool`` wrapper yields its ``tool_input``."""
    raw = _raw_input(update)
    inner = raw.get("tool_input")
    if isinstance(raw.get("tool_name"), str) and isinstance(inner, dict):
        return inner
    return raw


def _tool_output(update: dict[str, Any]) -> tuple[str, str | None]:
    raw = update.get("rawOutput") or update.get("raw_output") or update.get("output")
    diff: str | None = None
    content = update.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "diff" and isinstance(block.get("diff"), str):
                diff = block["diff"]
            elif isinstance(block.get("text"), str) and not raw:
                raw = block["text"]
    if isinstance(raw, dict):
        text = json_preview(raw)
    elif raw is None:
        text = ""
    else:
        text = str(raw)
    return text, diff


def json_preview(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, indent=2)[:8000]
    except TypeError:
        return str(value)


def _locations(update: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for loc in update.get("locations") or []:
        if isinstance(loc, dict) and isinstance(loc.get("path"), str):
            found.append(loc["path"])
        elif isinstance(loc, str):
            found.append(loc)
    raw = _tool_input(update)
    for key in ("path", "file_path", "target_file"):
        if isinstance(raw.get(key), str):
            found.append(raw[key])
    return found


async def grok_loop(
    session: Session,
    *,
    binary: str = "",
    yolo: bool = False,
    skip_all_fn: Callable[[], bool] | None = None,
    client: AcpClient | None = None,
    cu_command: str | list[str] = "",
) -> None:
    """Run until the session is cancelled.

    ``client`` is injectable so tests do not spawn a real ``grok``.
    """
    owned = client is None
    acp = client or AcpClient()
    grok_id: str | None = None
    started = False
    after_tool = False
    current_mode = ""
    current_modes: list[str] = []
    current_model: str | None = None
    image_ok = False

    async def emit_mode() -> None:
        if not current_mode and not current_model:
            return
        await session.event_log.add(
            GrokMode(
                session_id=session.id,
                mode=current_mode,
                modes=current_modes,
                model=current_model,
                seq=1,
            )
        )

    async def ensure_started() -> None:
        nonlocal grok_id, started, current_model, image_ok
        if started and acp.alive:
            return
        started = False
        if owned:
            path = find_grok_binary(binary)
            try:
                await acp.start(
                    grok_argv(path, yolo=yolo), cwd=session.workspace_path, env=grok_env()
                )
            except GrokEngineError as exc:
                if exc.code != "already_started":
                    raise
        from tstd import __version__

        init = await acp.initialize("tst-desk", __version__)
        image_ok = prompt_image_supported(init if isinstance(init, dict) else {})
        named = grok_model_from_payload(init if isinstance(init, dict) else None)
        if named:
            current_model = named
        resume = _load_grok_session_id(session)
        servers = acp_mcp_servers(cu_command, search_from=session.workspace_path)
        log.info(
            "grok mcp servers",
            extra={
                "extra_fields": {
                    "count": len(servers),
                    "names": [str(item.get("name")) for item in servers],
                    "commands": [Path(str(item.get("command", ""))).name for item in servers],
                }
            },
        )
        grok_id = await acp.session_new(
            session.workspace_path,
            yolo=yolo,
            resume_id=resume,
            mcp_servers=servers,
        )
        _save_grok_session_id(session, grok_id)
        session.grok_acp = acp
        session.grok_session_id = grok_id
        started = True
        await emit_mode()

    async def on_update(params: dict[str, Any]) -> None:
        nonlocal after_tool, current_model, current_mode, current_modes
        update = params.get("update") if isinstance(params.get("update"), dict) else params
        assert isinstance(update, dict)
        named = grok_model_from_payload(params) or grok_model_from_payload(update)
        if named and named != current_model:
            current_model = named
            await emit_mode()
        kind = str(update.get("sessionUpdate") or update.get("session_update") or "")
        if kind in {"agent_message_chunk", "agent_message"}:
            raw = _update_text(update)
            previous = ""
            if session.conversation and session.conversation[-1].role == "assistant":
                previous = session.conversation[-1].content or ""
            text = _visible_delta(previous, raw, after_tool=after_tool)
            after_tool = False
            if text:
                await session.event_log.add(
                    AssistantDelta(session_id=session.id, delta=text, seq=1)
                )
                _append_assistant(session, text)
                await _maybe_preview_text(session, text)
            return
        if kind in {"agent_thought_chunk", "agent_thought"}:
            text = _update_text(update)
            if text:
                await session.event_log.add(
                    AssistantReasoning(session_id=session.id, delta=text, seq=1)
                )
            return
        if kind == "tool_call":
            tool_call_id = str(
                update.get("toolCallId") or update.get("tool_call_id") or uuid.uuid4()
            )
            name = _tool_name(update)
            arguments = _tool_input(update)
            session.record_touched(_locations(update))
            await _maybe_preview_paths(session, _locations(update))
            await session.event_log.add(
                ToolCall(
                    session_id=session.id,
                    tool_call_id=tool_call_id,
                    name=name,
                    arguments=arguments,
                    decision_class="B",
                    seq=1,
                )
            )
            # Same episode tags as the native loop (TD-3407): the pane glow
            # and the daemon's real-display ring follow cu_session, which
            # is the only thing that can close them on this engine.
            await session.open_cu_session(name)
            after_tool = True
            return
        if kind == "tool_call_update":
            tool_call_id = str(update.get("toolCallId") or update.get("tool_call_id") or "unknown")
            status = str(update.get("status") or "completed")
            output, diff = _tool_output(update)
            session.record_touched(_locations(update))
            await _maybe_preview_paths(session, _locations(update))
            await _maybe_preview_text(session, output)
            if status in {"completed", "failed"}:
                await session.event_log.add(
                    ToolResult(
                        session_id=session.id,
                        tool_call_id=tool_call_id,
                        status="success" if status == "completed" else "error",
                        output=output or status,
                        diff=diff,
                        seq=1,
                    )
                )
                after_tool = True
            return
        if kind in {"available_commands", "available_commands_update"}:
            raw_cmds = update.get("availableCommands") or update.get("commands") or []
            commands: list[GrokCommand] = []
            if isinstance(raw_cmds, list):
                for item in raw_cmds:
                    if isinstance(item, dict) and item.get("name"):
                        commands.append(
                            GrokCommand(
                                name=str(item["name"]).lstrip("/"),
                                description=str(item.get("description") or ""),
                            )
                        )
            await session.event_log.add(
                GrokCommands(session_id=session.id, commands=commands, seq=1)
            )
            return
        if kind == "plan":
            entries: list[GrokPlanEntry] = []
            raw_entries = update.get("entries") or []
            if isinstance(raw_entries, list):
                for item in raw_entries:
                    if isinstance(item, dict) and item.get("content"):
                        entries.append(
                            GrokPlanEntry(
                                content=str(item["content"]),
                                status=str(item.get("status") or "pending"),
                            )
                        )
            markdown = str(update.get("markdown") or update.get("content") or "")
            await session.event_log.add(
                GrokPlan(session_id=session.id, markdown=markdown, entries=entries, seq=1)
            )
            return
        if kind in {"current_mode_update", "session_mode"}:
            mode = str(update.get("currentModeId") or update.get("mode") or "")
            available = update.get("availableModes") or update.get("modes") or []
            modes = (
                [str(m.get("id") if isinstance(m, dict) else m) for m in available]
                if isinstance(available, list)
                else []
            )
            if mode:
                current_mode = mode
                current_modes = modes
                await emit_mode()
            return

    async def on_permission(params: dict[str, Any]) -> dict[str, Any]:
        tool_call = params.get("toolCall") or params.get("tool_call") or {}
        if not isinstance(tool_call, dict):
            tool_call = {}
        tool_call_id = str(
            tool_call.get("toolCallId") or tool_call.get("tool_call_id") or uuid.uuid4()
        )
        name = _tool_name(tool_call)
        arguments = _tool_input(tool_call)
        raw_options = params.get("options")
        option_dicts = (
            [o for o in raw_options if isinstance(o, dict)] if isinstance(raw_options, list) else []
        )
        if yolo or (skip_all_fn is not None and skip_all_fn()):
            chosen = pick_permission_option(option_dicts, approved=True)
            return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}
        title = str(tool_call.get("title") or name)
        outcome = await session.request_acp_approval(
            tool_call_id,
            name,
            arguments,
            summary=title,
            reason="Grok agent requested approval before running this tool",
        )
        chosen = pick_permission_option(option_dicts, approved=outcome.approved)
        if outcome.approved:
            return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    acp.on_update(on_update)
    acp.on_permission(on_permission)

    try:
        while not session.cancel_requested:
            user_content = await session.wait_for_user_message()
            if user_content is None:
                break
            turn_id = str(uuid.uuid4())
            turn_start = time.time()
            session.conversation.append(ChatMessage(role="user", content=user_content))
            await session.event_log.add(
                UserTurn(
                    session_id=session.id,
                    turn_id=turn_id,
                    content=user_content,
                    seq=1,
                )
            )
            await session.conversation_changed()
            try:
                await ensure_started()
                assert grok_id is not None
                images = list(session.pending_images)
                session.pending_images = []
                prompt_text = user_content
                prompt_images = images
                files: list[tuple[str, str, str]] | None = None
                # Grok 1.0.13 drops ACP image blocks (image: false). Write
                # them into the workspace and pass a resource_link so the
                # agent can Read the file instead of crashing the NDJSON
                # reader on the echoed base64 line.
                if images and not image_ok:
                    saved = persist_prompt_images(
                        session.workspace_path, session.id, images
                    )
                    notes = "\n".join(
                        f"--- attached image: {name} ({media}) at {rel} ---"
                        for name, rel, media, _uri in saved
                    )
                    prompt_text = f"{user_content}\n\n{notes}" if user_content else notes
                    files = [(name, uri, media) for name, _rel, media, uri in saved]
                    prompt_images = None
                prompt_kwargs: dict[str, Any] = {}
                if files:
                    prompt_kwargs["files"] = files
                prompt_task = asyncio.create_task(
                    acp.prompt(grok_id, prompt_text, prompt_images, **prompt_kwargs)
                )
                followup = False
                while not prompt_task.done():
                    followup = session.pending_user_messages > 0
                    if session.cancel_requested or followup:
                        await _interrupt_prompt(
                            acp,
                            prompt_task,
                            grok_id,
                            kill=owned,
                        )
                        if owned and not acp.alive:
                            started = False
                            grok_id = None
                        break
                    await asyncio.sleep(0.05)
                if session.cancel_requested:
                    break
                if followup:
                    # Do not emit turn_complete: the next queued message is
                    # the interruption and must start a new prompt. The
                    # interrupted turn's computer-use episode is over.
                    await session.close_cu_session()
                    continue
                result: dict[str, Any] = {}
                if not prompt_task.cancelled():
                    try:
                        result = await prompt_task
                    except GrokEngineError as exc:
                        await _fail_turn(session, turn_start, exc)
                        continue
                tokens, cost = _usage_from_result(result)
                await session.event_log.add(
                    CostUpdate(
                        session_id=session.id,
                        turn_cost=cost,
                        session_cost=cost,
                        total_cost=cost,
                        seq=1,
                    )
                )
                await session.event_log.add(
                    TurnComplete(
                        session_id=session.id,
                        tokens=tokens,
                        cost=cost,
                        tier="brain",
                        duration=time.time() - turn_start,
                        seq=1,
                    )
                )
                await session.close_cu_session()
                await session.conversation_changed()
            except GrokEngineError as exc:
                await _fail_turn(session, turn_start, exc)
    finally:
        await session.close_cu_session()
        session.grok_acp = None
        if owned:
            await acp.close()
        if session.cancel_requested and grok_id is not None:
            with contextlib.suppress(GrokEngineError):
                await acp.cancel(grok_id)


async def _interrupt_prompt(
    acp: AcpClient,
    prompt_task: asyncio.Task[Any],
    grok_id: str | None,
    *,
    kill: bool,
) -> None:
    """Ask Grok to abort the current prompt; kill the process if it will not."""
    if grok_id is not None:
        with contextlib.suppress(GrokEngineError):
            await acp.cancel(grok_id)
    try:
        await asyncio.wait_for(prompt_task, timeout=PROMPT_CANCEL_GRACE_S)
        return
    except TimeoutError:
        prompt_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, GrokEngineError):
            await prompt_task
    except (asyncio.CancelledError, GrokEngineError):
        return
    if kill:
        await acp.close()


def _usage_from_result(result: dict[str, Any]) -> tuple[int, float]:
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    assert isinstance(usage, dict)
    tokens = 0
    for key in ("total_tokens", "totalTokens", "input_tokens", "inputTokens"):
        value = usage.get(key)
        if isinstance(value, int):
            tokens = max(tokens, value)
    cost = 0.0
    for key in ("total_cost_usd", "costUSD", "cost"):
        value = usage.get(key) if usage else result.get(key)
        if isinstance(value, (int, float)):
            cost = float(value)
            break
    return tokens, cost


async def _maybe_preview_text(session: Session, text: str) -> None:
    url = sniff_local_url(text)
    if url:
        await session.event_log.add(
            GrokPreview(
                session_id=session.id,
                kind="url",
                url=url,
                title=url,
                seq=1,
            )
        )
        return
    for raw in text.split():
        kind = media_kind(raw.strip("\"'"))
        if kind:
            await _maybe_preview_paths(session, [raw.strip("\"'")])


async def _maybe_preview_paths(session: Session, paths: list[str]) -> None:
    for raw in paths:
        kind = media_kind(raw)
        if kind is None:
            continue
        resolved = resolve_workspace_file(session.workspace_path, raw)
        if resolved is None:
            continue
        preview_kind: str = kind
        await session.event_log.add(
            GrokPreview(
                session_id=session.id,
                kind=preview_kind,  # type: ignore[arg-type]
                path=str(resolved),
                title=resolved.name,
                seq=1,
            )
        )


def _append_assistant(session: Session, text: str) -> None:
    if session.conversation and session.conversation[-1].role == "assistant":
        last = session.conversation[-1]
        session.conversation[-1] = ChatMessage(
            role="assistant", content=(last.content or "") + text
        )
    else:
        session.conversation.append(ChatMessage(role="assistant", content=text))


async def _fail_turn(session: Session, turn_start: float, exc: GrokEngineError) -> None:
    # Engine faults are Error events + a failed turn_complete. They are not
    # assistant prose — putting them in assistant_delta is what printed
    # "ACP client already started" in the chat bubble.
    message = exc.message
    await session.event_log.add(Error(session_id=session.id, code=exc.code, message=message, seq=1))
    await session.conversation_changed()
    await session.event_log.add(
        TurnComplete(
            session_id=session.id,
            tokens=0,
            cost=0.0,
            tier="brain",
            duration=time.time() - turn_start,
            failed=True,
            error_code=exc.code,
            seq=1,
        )
    )
    await session.close_cu_session()
    log.warning(
        "grok turn failed",
        extra={"extra_fields": {"session_id": session.id, "code": exc.code}},
    )
