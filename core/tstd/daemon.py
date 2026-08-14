"""TST Desk daemon — async process with graceful lifecycle.

The daemon owns sessions, the WebSocket server, and the event loop.
It is spawned by the Tauri host and communicates via a local WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import websockets.exceptions

from .audit import AuditStore
from .audit_writer import AuditWriter
from .boundary_config import (
    boundary_source,
    load_workspace_boundary,
    scaffold_workspace_config,
)
from .config import ConfigError, ModelConfig, cached_config, save_active_preset
from .context.assembler import ContextAssembler
from .context.prompt import PromptAssembler
from .context.stack import build_instruction_stack
from .keychain import KeychainError, get_api_key, store_api_key
from .logging import get_logger, setup_logging, user_data_dir
from .loop import ProviderLike, agent_loop
from .policy import (
    add_rule,
    load_approved_imports,
    load_policy,
    propose_always_allow,
    remove_rule,
    save_policy,
)
from .protocol import (
    AlwaysAllow,
    ApiKeyValidated,
    Approve,
    Attach,
    Cancel,
    ClientMessageT,
    DaemonEvent,
    Deny,
    Detach,
    DiagnosticCheck,
    DiagnosticsReport,
    GetInstructionStack,
    GetSetupState,
    HandshakeError,
    ListPolicyRules,
    ListSessions,
    OpenWorkspace,
    PolicyRules,
    PolicyRuleSummary,
    Resume,
    RevokePolicyRule,
    RunDiagnostics,
    SessionList,
    SessionSummary,
    SetApiKey,
    SetPreset,
    SetTier,
    SetupState,
    Shutdown,
    TierState,
    UserMessage,
    ValidateApiKey,
    build_error,
    parse_client_message,
)
from .protocol import (
    BoundaryUpdate as BoundaryUpdateEvent,
)
from .protocol import (
    SessionState as SessionStateEvent,
)
from .protocol import (
    TierSwitched as TierSwitchedEvent,
)
from .provider import (
    ChatCompletionRequest,
    ChatMessage,
    ProviderClient,
    ProviderError,
    auth_failure_message,
)
from .router import TIER_NAMES, TierRouter
from .session import Session, SessionEventLog, SessionRegistry, SessionRunner
from .session_store import SessionStore
from .tools import ToolDispatcher, create_registry, register_builtin_handlers
from .ws import WebSocketServer

log = get_logger("tstd.daemon")


@dataclass
class DaemonState:
    """Mutable state tracked by the daemon."""

    started_at: float = field(default_factory=time.time)
    active_sessions: int = 0

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at


def _parent_alive(pid: int) -> bool:
    """Return True if the process with the given PID is alive.

    Used by the orphan-prevention watchdog. Kept cross-platform even though
    this story exercises macOS only:
      - POSIX: ``os.kill(pid, 0)`` probes liveness without signalling.
      - Windows: a limited OpenProcess probe (best-effort).
    """
    if os.name == "nt":
        return _win_parent_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not ours — still alive.
        return True
    return True


def _win_parent_alive(pid: int) -> bool:
    """Probe a Windows process with a limited-query handle (best-effort).

    OpenProcess alone is not enough: a dead process's object persists while
    any handle to it is open, so the open succeeding does not mean the
    process lives.  GetExitCodeProcess distinguishes — anything other than
    STILL_ACTIVE (259) means the process has exited.
    """
    try:
        import ctypes

        # ``ctypes.windll`` exists only on Windows and is absent from mypy's
        # POSIX stubs; getattr keeps one spelling clean on every platform.
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return True  # unreachable — caller gates on os.name == "nt"
        process_query_limited = 0x1000
        still_active = 259
        handle = windll.kernel32.OpenProcess(process_query_limited, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if not windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # query failed — assume alive rather than orphan the session
            return exit_code.value == still_active
        finally:
            windll.kernel32.CloseHandle(handle)
    except Exception:
        # Degrade to "alive" so a watchdog bug never spuriously kills us.
        return True


def _check_git() -> DiagnosticCheck:
    """git reachable on PATH (TD-1104). Runs in a worker thread."""
    path = shutil.which("git")
    if path is None:
        fix = (
            "Install Xcode Command Line Tools (`xcode-select --install`)."
            if sys.platform == "darwin"
            else "Install git and make sure it is on PATH."
        )
        return DiagnosticCheck(name="git", status="fail", detail="git not found on PATH", fix=fix)
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        version = proc.stdout.strip() or "version unknown"
    except (OSError, subprocess.TimeoutExpired):
        version = "version probe failed"
    return DiagnosticCheck(name="git", status="ok", detail=f"{version} ({path})")


def _check_writable(workspace: Path) -> DiagnosticCheck:
    """The workspace accepts a file (TD-1104). Runs in a worker thread.

    Rows travel the wire and the UI's copy-to-clipboard report, so the
    detail names the workspace by its basename — never an absolute path.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=workspace, prefix=".tstd-probe-", delete=True):
            pass
    except OSError as e:
        return DiagnosticCheck(
            name="workspace",
            status="fail",
            detail=f"{workspace.name} is not writable: {e.strerror or e}",
            fix="Fix the folder's permissions or open a different workspace.",
        )
    return DiagnosticCheck(name="workspace", status="ok", detail=f"{workspace.name} is writable")


def _relativize_paths(text: str, workspace: Path) -> str:
    """Keep absolute local paths out of diagnostics rows (TD-1104).

    Import issues embed resolved (symlink-normalized) absolute paths; rows
    travel the wire and end up in the copy-to-clipboard report, so
    workspace paths become relative and home paths become ``~/…`` — still
    enough to locate the file. The workspace prefix is replaced first (it
    usually sits under home, and the longer prefix must win).
    """
    root = workspace.resolve()
    home = Path.home().resolve()
    return text.replace(f"{root}{os.sep}", "").replace(f"{home}{os.sep}", f"~{os.sep}")


def _approved_import_allowlist(workspace: str | Path) -> frozenset[Path]:
    """Durable external-import allowlist for read-only inspectors (TD-505).

    Denied imports are session-scoped by design, so inspectors forward only
    the persisted set — a denied path correctly reads as "would prompt on a
    new session".  A malformed config yields an empty allowlist rather than
    failing the inspection.
    """
    try:
        return frozenset(load_approved_imports(workspace))
    except ConfigError:
        return frozenset()


def _tier_state_event(session_id: str, router: TierRouter, config: ModelConfig) -> TierState:
    """Build the ``tier_state`` event for the title bar (TD-1006)."""
    return TierState(
        session_id=session_id,
        tier=router.active_tier,
        override=router.override,
        model_slugs={name: config.tier(name).slug for name in TIER_NAMES},
        seq=1,  # overwritten by the event log
    )


class Daemon:
    """Async daemon process with clean startup and shutdown.

    Usage:
        daemon = Daemon()
        await daemon.run()
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        provider: ProviderLike | None = None,
        parent_pid: int | None = None,
    ) -> None:
        self.data_dir = data_dir or user_data_dir()
        self.state = DaemonState()
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self.session_registry = SessionRegistry()
        self._session_store = SessionStore(self.data_dir)
        self.config = cached_config()
        self._provider = provider
        # Built when the daemon starts serving — the audit database only
        # appears on disk once the daemon actually runs (TD-902).
        self._audit_writer: AuditWriter | None = None
        # When set, a watchdog task shuts the daemon down if this host dies.
        self._parent_pid = parent_pid
        self._parent_poll_interval = 1.0
        # session_id -> set of attached connections
        self._attached_clients: dict[str, set[Any]] = {}
        # (connection, session_id) -> streaming task
        self._streaming_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self.ws_server = WebSocketServer(
            self.data_dir,
            message_handler=self._handle_message,
            on_disconnect=self._on_connection_closed,
        )

    async def _ensure_provider(self) -> ProviderLike:
        """Create the shared provider client on first use.

        Uses the brain tier's base URL for the OpenAI-compatible endpoint;
        the API key comes from the OS keychain.
        """
        if self._provider is None:
            tier_cfg = self.config.tier("brain")
            self._provider = await ProviderClient.from_keychain(tier_cfg.base_url)
        return self._provider

    async def _setup_state_event(self) -> SetupState:
        """Current onboarding state (TD-1101): key presence + preset choice.

        ``has_api_key`` is the wizard's first-run signal; presence is probed
        from the keychain, so it survives daemon restarts and never touches
        the key value itself.
        """
        try:
            await get_api_key()
            has_api_key = True
        except KeychainError:
            has_api_key = False
        return SetupState(
            seq=1,
            has_api_key=has_api_key,
            presets=sorted(self.config.presets),
            active_preset=self.config.active_preset,
        )

    async def _provider_probe(self) -> ProviderError | None:
        """One-token live call against the active brain (TD-1101).

        Returns None on success, the ProviderError on failure.  Raises
        KeychainError when no key is stored.
        """
        tier_cfg = self.config.tier("brain")
        client = await ProviderClient.from_keychain(tier_cfg.base_url)
        response = await client.chat_completion(
            ChatCompletionRequest(
                model=tier_cfg.slug,
                messages=[ChatMessage(role="user", content="ok")],
                max_tokens=1,
                stream=False,
            )
        )
        return response if isinstance(response, ProviderError) else None

    async def _validate_api_key(self) -> ApiKeyValidated:
        """Probe the stored key with one cheap live call (TD-1101).

        A one-token completion against the active preset's brain tier: the
        cheapest request that still proves the key authenticates.  The key
        value never appears in the response — success names the keychain
        account, failure carries actionable text.
        """
        try:
            err = await self._provider_probe()
        except KeychainError as e:
            return ApiKeyValidated(seq=1, ok=False, detail=str(e))
        if err is not None:
            detail = auth_failure_message() if err.code == "auth_failed" else err.message
            return ApiKeyValidated(seq=1, ok=False, detail=detail)
        return ApiKeyValidated(
            seq=1, ok=True, detail="Key accepted by the active preset's provider."
        )

    # ── Diagnostics (TD-1104 doctor) ──────────────────────────────────

    def _current_workspace(self) -> Path | None:
        """Workspace of the most recently used session, if any."""
        records = self._session_store.records()
        if not records:
            return None
        return Path(max(records, key=lambda r: r.updated_at).workspace_path)

    async def _doctor_key_provider_rows(self) -> list[DiagnosticCheck]:
        """The ``api_key`` and ``provider`` rows from one live probe.

        Both AC rows share a single one-token call: an auth failure means
        the provider IS reachable (it answered) but the key is bad; a
        transport failure means we never got far enough to judge the key.
        """
        fix_key = "Re-enter a valid key: title-bar gear → Provider API key."
        try:
            err = await self._provider_probe()
        except KeychainError as e:
            return [
                DiagnosticCheck(name="api_key", status="fail", detail=str(e), fix=fix_key),
                DiagnosticCheck(
                    name="provider",
                    status="skip",
                    detail="not checked — no API key to send",
                ),
            ]

        base_url = self.config.tier("brain").base_url
        if err is None:
            return [
                DiagnosticCheck(name="api_key", status="ok", detail="accepted by the provider"),
                DiagnosticCheck(name="provider", status="ok", detail=f"reachable at {base_url}"),
            ]
        if err.code in ("connection_error", "timeout"):
            return [
                DiagnosticCheck(
                    name="api_key",
                    status="skip",
                    detail="cannot validate — the provider is unreachable",
                ),
                DiagnosticCheck(
                    name="provider",
                    status="fail",
                    detail=f"unreachable at {base_url} ({err.code})",
                    fix="Check the network or VPN, and the provider base_url in config.yaml.",
                ),
            ]
        if err.code == "auth_failed":
            return [
                DiagnosticCheck(
                    name="api_key",
                    status="fail",
                    detail="provider rejected the stored key (401)",
                    fix=fix_key,
                ),
                DiagnosticCheck(name="provider", status="ok", detail=f"reachable at {base_url}"),
            ]
        # Any other error still means the provider answered.
        return [
            DiagnosticCheck(name="api_key", status="ok", detail="accepted by the provider"),
            DiagnosticCheck(
                name="provider", status="ok", detail=f"answered at {base_url} ({err.code})"
            ),
        ]

    async def _diagnostics_report(self) -> DiagnosticsReport:
        """Run the doctor checks (TD-1104) and return the report.

        Rows in display order: daemon → key → provider → git → workspace →
        steering.  Blocking filesystem calls ride worker threads; the live
        provider probe is the only slow row (one token, worst case the
        provider timeout).
        """
        checks: list[DiagnosticCheck] = [
            # The reply itself proves reachability; the row carries versions
            # so a pasted report is self-describing.
            DiagnosticCheck(
                name="daemon",
                status="ok",
                detail=f"responding (v{self._version()}, up {self.state.uptime:.1f}s)",
            )
        ]

        # API key presence first — presence is free, validity needs the probe.
        try:
            await get_api_key()
            key_present = True
        except KeychainError:
            key_present = False
        if key_present:
            checks.extend(await self._doctor_key_provider_rows())
        else:
            checks.append(
                DiagnosticCheck(
                    name="api_key",
                    status="fail",
                    detail="no API key stored",
                    fix="Open the setup wizard (title-bar gear) and paste an API key.",
                )
            )
            checks.append(
                DiagnosticCheck(
                    name="provider",
                    status="skip",
                    detail="not checked — no API key to send",
                )
            )

        checks.append(await asyncio.to_thread(_check_git))

        workspace = self._current_workspace()
        if workspace is None:
            checks.append(
                DiagnosticCheck(name="workspace", status="skip", detail="no workspace open yet")
            )
            checks.append(
                DiagnosticCheck(name="steering", status="skip", detail="no workspace open yet")
            )
        else:
            checks.append(await asyncio.to_thread(_check_writable, workspace))
            checks.append(await self._check_steering(workspace))

        return DiagnosticsReport(seq=1, checks=checks)

    async def _check_steering(self, workspace: Path) -> DiagnosticCheck:
        """Steering stack parses: resolution runs and imports land."""
        try:
            assembled = await ContextAssembler().assemble(
                workspace, approved_imports=_approved_import_allowlist(workspace)
            )
        except Exception as e:  # resolution is designed not to raise; report if it does
            return DiagnosticCheck(
                name="steering",
                status="fail",
                detail=_relativize_paths(f"steering resolution failed: {e}", workspace),
                fix="Fix the malformed steering file and re-run Doctor.",
            )
        issues = assembled.import_issues
        if issues:
            more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
            return DiagnosticCheck(
                name="steering",
                status="fail",
                detail=_relativize_paths(f"{issues[0]}{more}", workspace),
                fix="Fix or remove the broken @import in the named file.",
            )
        return DiagnosticCheck(
            name="steering",
            status="ok",
            detail=f"{len(assembled.sources)} steering source(s) parsed",
        )

    async def run(self) -> None:
        """Start the daemon and run until shutdown is requested."""
        log.info(
            "starting",
            extra={"extra_fields": {"version": self._version(), "data_dir": str(self.data_dir)}},
        )

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Rehydrate the session list from the durable snapshot before we
        # start serving, so a client connecting after a crash sees the
        # same sessions it had (marked interrupted where they were live).
        await self._restore_sessions()

        # Register signal handlers.  asyncio's add_signal_handler is
        # POSIX-only — it raises NotImplementedError on Windows, where
        # shutdown still arrives via the shutdown message, the parent
        # watchdog, or the console control event the host sends.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._on_signal)

        # If the host told us its PID, watch it: if the host dies (including
        # by force-quit) we must not become an orphan.
        if self._parent_pid is not None:
            self._tasks.append(asyncio.create_task(self._parent_watchdog(self._parent_pid)))

        # Start subsystems
        try:
            await self._serve()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

        log.info(
            "stopped",
            extra={"extra_fields": {"uptime": f"{self.state.uptime:.1f}s"}},
        )

    async def _serve(self) -> None:
        """Main serving loop — start subsystems and wait for shutdown."""
        self._audit_writer = AuditWriter(AuditStore(self.data_dir / "audit.db"))
        self._audit_writer.start()
        await self.ws_server.start()
        await self._shutdown_event.wait()

    async def _shutdown(self) -> None:
        """Graceful shutdown: cancel tasks, close sockets, flush state."""
        log.info("shutting down")

        # Stop the WebSocket server (closes clients and removes the port file)
        await self.ws_server.stop()

        # Stop session loops
        sessions = await self.session_registry.list_sessions()
        for session in sessions:
            runner = self.session_registry.get_runner(session.id)
            if runner is not None:
                await runner.cancel()

        # Cancel streaming tasks
        for task in list(self._streaming_tasks.values()):
            if not task.done():
                task.cancel()
        if self._streaming_tasks:
            await asyncio.gather(*self._streaming_tasks.values(), return_exceptions=True)
            self._streaming_tasks.clear()

        # Cancel remaining daemon tasks (e.g. the parent watchdog)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        # Drain audit writes before closing the store (state flushed)
        if self._audit_writer is not None:
            await self._audit_writer.close()
            self._audit_writer = None

        log.info("shutdown complete")

    def _on_signal(self) -> None:
        """Handle OS signals for graceful shutdown."""
        log.info("signal received")
        self._shutdown_event.set()

    async def _restore_sessions(self) -> None:
        """Rehydrate the registry from the durable session snapshot.

        Sessions that were terminal when the daemon last ran keep their
        terminal state; anything still alive is marked ``interrupted`` (its
        event log did not survive, so it can never resume).
        """
        terminal = {"complete", "failed", "cancelled"}
        for record in self._session_store.records():
            final_state = record.state if record.state in terminal else "interrupted"
            await self.session_registry.restore(
                record.session_id, record.workspace_path, final_state
            )
            if final_state != record.state:
                await self._session_store.update_state(record.session_id, final_state)
            log.info(
                "restored session",
                extra={
                    "extra_fields": {
                        "session_id": record.session_id,
                        "state": final_state,
                    }
                },
            )

    async def _parent_watchdog(self, parent_pid: int) -> None:
        """Poll the host's liveness and shut down if it dies (TD-1002).

        The host can be force-killed in a way no destructor catches, so the
        daemon must notice on its own. ``--parent-pid`` is the contract that
        makes this testable without the real Tauri host.
        """
        while not self._shutdown_event.is_set():
            if not _parent_alive(parent_pid):
                log.warning("parent process gone, shutting down to avoid orphan")
                self._shutdown_event.set()
                return
            await asyncio.sleep(self._parent_poll_interval)

    async def _on_session_event(self, event: DaemonEvent, _log: SessionEventLog) -> None:
        """Persist session state transitions so the list survives restart."""
        if isinstance(event, SessionStateEvent):
            await self._session_store.update_state(event.session_id, event.state)

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__

    async def _handle_message(self, raw: str, _connection: Any) -> str | None:
        """Handle a post-handshake message from a client.

        Routes messages to the session layer. Returns a response string
        or None to send nothing.
        """
        try:
            msg: ClientMessageT = parse_client_message(raw)
        except HandshakeError as e:
            return build_error(e.code, e.message)

        if isinstance(msg, OpenWorkspace):
            # Validate before creating anything (TD-1103): a nonexistent or
            # non-directory path must not silently "succeed".
            workspace_path = Path(msg.path)
            # Blocking stat off the event loop (ASYNC240 precedent: TD-1104
            # diagnostics use to_thread for the same reason).
            if not await asyncio.to_thread(workspace_path.is_dir):
                return build_error(
                    "workspace_not_found",
                    f"Workspace path is not a directory: {msg.path}",
                )
            # Plant the commented config template on first open (TD-1103) —
            # never overwrites an existing config.
            await asyncio.to_thread(scaffold_workspace_config, workspace_path)
            sess = await self.session_registry.create(msg.path)
            # Persist the new session and keep its state durable going forward.
            await self._session_store.upsert(sess.id, msg.path, sess.state)
            # Bound method vs the ``__call__``-shaped EventSubscriber protocol:
            # mypy can't confirm the shapes line up, though they are identical.
            sess.event_log.subscribe(self._on_session_event)  # type: ignore[arg-type]
            router = TierRouter()
            # So a later set_tier reaches the loop's router (TD-1006).
            sess.router = router

            # Workspace boundary (TD-706): resolve `.tst/config.yaml` or
            # defaults; a bad config falls back to defaults with the
            # actionable error logged and surfaced in the event source.
            try:
                sess.boundary_config = load_workspace_boundary(msg.path)
                source = boundary_source(msg.path)
            except ConfigError as e:
                log.warning(
                    "workspace boundary config invalid; using defaults",
                    extra={"extra_fields": {"workspace_path": msg.path, "error": str(e)}},
                )
                source = f"defaults — invalid config ({e})"

            # Approval policy (TD-801/802): same file, policy: section;
            # an invalid section falls back to rule-free defaults.
            try:
                sess.policy = load_policy(msg.path)
            except ConfigError as e:
                log.warning(
                    "workspace policy config invalid; using defaults",
                    extra={"extra_fields": {"workspace_path": msg.path, "error": str(e)}},
                )

            # Provider is created lazily via a factory closure so that
            # sessions can be opened and attached without requiring a key
            # to be present.  The provider is only needed when the loop
            # processes its first user message.
            async def get_provider() -> ProviderLike:
                return await self._ensure_provider()

            # Tool stack (TD-604/605, TD-1401): the daemon hands every
            # session the builtin registry + dispatcher; the loop attaches
            # the classifier, path guard, and checkpointer on first turn.
            tool_registry = create_registry()
            tool_dispatcher = ToolDispatcher(tool_registry)
            register_builtin_handlers(
                tool_dispatcher,
                allowed_commands=tuple(sess.boundary_config.boundary.allowed_commands),
            )

            sink = self._audit_writer
            runner = SessionRunner(
                sess,
                loop_factory=lambda s: agent_loop(
                    s,
                    router,
                    get_provider,
                    self.config,
                    tool_registry=tool_registry,
                    tool_dispatcher=tool_dispatcher,
                    audit_sink=sink,
                ),
            )
            await runner.start()
            if self._audit_writer is not None:
                self._audit_writer.attach_session(sess)
            await self.session_registry.register_runner(sess.id, runner)
            self.state.active_sessions += 1
            log.info(
                "session opened",
                extra={
                    "extra_fields": {
                        "session_id": sess.id,
                        "workspace_path": msg.path,
                    }
                },
            )

            # Emit the resolved boundary after session_state (seq 1) so
            # the client sees the wall it opened under (TD-706).
            cfg = sess.boundary_config
            await sess.event_log.add(
                BoundaryUpdateEvent(
                    session_id=sess.id,
                    writable_paths=list(cfg.boundary.writable_paths),
                    allowed_commands=list(cfg.boundary.allowed_commands),
                    network=cfg.boundary.network,
                    spend_usd=cfg.caps.spend_usd,
                    wall_clock_hours=cfg.caps.wall_clock_hours,
                    max_iterations=cfg.caps.max_iterations,
                    source=source,
                    seq=1,
                )
            )

            # Emit the initial tier state (TD-1006) so the title bar can
            # render its chips with the configured slugs before the first
            # turn runs.
            await sess.event_log.add(_tier_state_event(sess.id, router, self.config))

            # Return the session_state event (seq=1, "running")
            events = sess.event_log.events_from(1)
            if events:
                return events[0].model_dump_json()
            return None

        if isinstance(msg, UserMessage):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            await found.add_user_message(msg.content)
            log.info(
                "user message enqueued",
                extra={
                    "extra_fields": {
                        "session_id": msg.session_id,
                        "content_length": len(msg.content),
                    }
                },
            )
            return None

        if isinstance(msg, Cancel):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            await self.session_registry.cancel(msg.session_id)
            self.state.active_sessions = max(0, self.state.active_sessions - 1)
            # Return the cancellation event
            events = found.event_log.events_from(found.event_log.last_seq)
            if events:
                return events[0].model_dump_json()
            return None

        if isinstance(msg, Resume):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            # Reload the workspace boundary so raised caps take effect;
            # the loop re-checks caps before its next model call and
            # re-pauses if the new caps are still exceeded.
            try:
                found.boundary_config = load_workspace_boundary(found.workspace_path)
            except ConfigError as e:
                log.warning(
                    "boundary config invalid on resume; keeping current caps",
                    extra={"extra_fields": {"session_id": msg.session_id, "error": str(e)}},
                )
            await found.resume()
            cfg = found.boundary_config
            await found.event_log.add(
                BoundaryUpdateEvent(
                    session_id=found.id,
                    writable_paths=list(cfg.boundary.writable_paths),
                    allowed_commands=list(cfg.boundary.allowed_commands),
                    network=cfg.boundary.network,
                    spend_usd=cfg.caps.spend_usd,
                    wall_clock_hours=cfg.caps.wall_clock_hours,
                    max_iterations=cfg.caps.max_iterations,
                    source="resume",
                    seq=1,
                )
            )
            return None

        if isinstance(msg, SetTier):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            if found.router is None:
                return build_error(
                    "session_not_live",
                    f"Session {msg.session_id!r} has no live agent loop "
                    "(restored after restart); its tier cannot be changed",
                )
            try:
                previous = found.router.active_tier
                found.router.set_tier(msg.tier)
            except ValueError as e:
                return build_error("bad_request", str(e))
            # Timeline entry for the manual switch (TD-1005)...
            await found.event_log.add(
                TierSwitchedEvent(
                    session_id=found.id,
                    tier=msg.tier,
                    previous=previous,
                    seq=1,  # overwritten by event log
                )
            )
            # ...then acknowledge with the new state so the title bar snaps
            # over even before the next turn starts (TD-1006).
            await found.event_log.add(_tier_state_event(found.id, found.router, self.config))
            log.info(
                "tier override set",
                extra={
                    "extra_fields": {
                        "session_id": found.id,
                        "tier": msg.tier,
                    }
                },
            )
            return None

        if isinstance(msg, (Approve, Deny)):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            # Resolution reaches the parked dispatcher via the
            # session-owned future; the session stays parked regardless
            # of which client (or none) is attached (TD-802).
            approved = isinstance(msg, Approve)
            detail = msg.reason if isinstance(msg, Deny) else None
            if not found.resolve_approval(msg.tool_call_id, approved, detail):
                return build_error(
                    "no_pending_approval",
                    f"No pending approval {msg.tool_call_id!r} in session {msg.session_id!r}",
                )
            return None

        if isinstance(msg, AlwaysAllow):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            pending = found.get_pending_approval(msg.tool_call_id)
            if pending is None:
                return build_error(
                    "no_pending_approval",
                    f"No pending approval {msg.tool_call_id!r} in session {msg.session_id!r}",
                )
            # External-import approvals (TD-505) have no tool; they are
            # class-C reads and can never be always-allowed (TD-803 c4).
            if pending.tool is None:
                return build_error(
                    "class_c_not_always_allowable",
                    "External imports can never be always-allowed",
                )
            # The narrowest rule, never a blanket grant; None when the
            # call is a class-C decision (TD-803 criterion 4).
            rule = propose_always_allow(
                pending.tool,
                pending.arguments,
                pending.decision_class,
                Path(found.workspace_path),
            )
            if rule is None:
                return build_error(
                    "class_c_not_always_allowable",
                    "Class C actions can never be always-allowed",
                )
            found.policy = add_rule(found.policy, rule)
            try:
                save_policy(found.workspace_path, found.policy)
            except ConfigError as e:
                return build_error("policy_save_failed", f"Failed to save policy: {e}")
            # Resolve the parked call as approved so the loop proceeds.
            found.resolve_approval(msg.tool_call_id, True)
            return None

        if isinstance(msg, ListPolicyRules):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            return PolicyRules(
                seq=1,
                rules=[
                    PolicyRuleSummary(tool=r.tool, args=r.args, effect=r.effect)
                    for r in found.policy.rules
                ],
            ).model_dump_json()

        if isinstance(msg, RevokePolicyRule):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            if not remove_rule(found.policy, msg.tool, msg.args):
                return build_error(
                    "policy_rule_not_found",
                    f"No policy rule {msg.tool!r}: {msg.args!r} to revoke",
                )
            try:
                save_policy(found.workspace_path, found.policy)
            except ConfigError as e:
                return build_error("policy_save_failed", f"Failed to save policy: {e}")
            return PolicyRules(
                seq=1,
                rules=[
                    PolicyRuleSummary(tool=r.tool, args=r.args, effect=r.effect)
                    for r in found.policy.rules
                ],
            ).model_dump_json()

        if isinstance(msg, Attach):
            found = self.session_registry.get(msg.session_id)
            if found is None:
                return build_error(
                    "session_not_found",
                    f"Session {msg.session_id!r} not found",
                )
            return await self._handle_attach(msg, found, _connection)

        if isinstance(msg, Detach):
            return await self._handle_detach(msg, _connection)

        if isinstance(msg, ListSessions):
            return await self._handle_list_sessions()

        if isinstance(msg, GetInstructionStack):
            return await self._handle_get_instruction_stack(msg)

        # ── Onboarding (TD-1101 first-run wizard) ────────────────────
        if isinstance(msg, GetSetupState):
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, SetApiKey):
            try:
                await store_api_key(msg.api_key)
            except (KeychainError, NotImplementedError) as e:
                return build_error("key_store_failed", f"Could not store the API key: {e}")
            # Never log the key; the ack is a refreshed setup_state.
            log.info("api key stored in keychain")
            return (await self._setup_state_event()).model_dump_json()

        if isinstance(msg, ValidateApiKey):
            return (await self._validate_api_key()).model_dump_json()

        if isinstance(msg, SetPreset):
            if msg.name not in self.config.presets:
                return build_error(
                    "unknown_preset",
                    f"Unknown preset {msg.name!r}; declared: "
                    f"{', '.join(sorted(self.config.presets))}",
                )
            try:
                save_active_preset(msg.name)
            except ConfigError as e:
                return build_error("bad_request", str(e))
            # New sessions route on the new preset immediately; existing
            # sessions keep the tier slugs they opened with.  model_copy
            # keeps the process-wide cached instance untouched.
            self.config = self.config.model_copy(update={"active_preset": msg.name})
            log.info(
                "active preset changed",
                extra={"extra_fields": {"preset": msg.name}},
            )
            return (await self._setup_state_event()).model_dump_json()

        # ── Diagnostics (TD-1104 doctor) ─────────────────────────────
        if isinstance(msg, RunDiagnostics):
            return (await self._diagnostics_report()).model_dump_json()

        if isinstance(msg, Shutdown):
            log.info("shutdown requested via websocket")
            self._shutdown_event.set()
            return None

        return None

    async def _handle_attach(self, msg: Attach, session: Session, connection: Any) -> str | None:
        """Handle an attach: replay events from from_seq, then stream live."""
        # Replay events from from_seq
        for event in session.event_log.events_from(msg.from_seq):
            await connection.send(event.model_dump_json())

        # Register as attached
        session_id = session.id
        if session_id not in self._attached_clients:
            self._attached_clients[session_id] = set()
        self._attached_clients[session_id].add(connection)

        # Start a background task that streams new events
        conn_key = (id(connection), session_id)

        async def _stream() -> None:
            seen_seq = session.event_log.last_seq
            try:
                while True:
                    new_seq = await session.event_log.wait_for_new_event(seen_seq)
                    for event in session.event_log.events_from(seen_seq + 1):
                        await connection.send(event.model_dump_json())
                    seen_seq = new_seq
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                # Clean up on disconnect
                self._cleanup_attach(connection, session_id, conn_key)

        task = asyncio.create_task(_stream())
        self._streaming_tasks[conn_key] = task
        return None

    async def _handle_list_sessions(self) -> str:
        """Build the durable session list as a ``session_list`` event."""
        summaries: list[SessionSummary] = []
        for record in self._session_store.records():
            sess = self.session_registry.get(record.session_id)
            summaries.append(
                SessionSummary(
                    session_id=record.session_id,
                    workspace_path=record.workspace_path,
                    state=cast(Any, record.state),
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    event_count=sess.event_log.last_seq if sess else 0,
                )
            )
        return SessionList(seq=1, sessions=summaries).model_dump_json()

    async def _handle_get_instruction_stack(self, msg: GetInstructionStack) -> str:
        """Assemble and answer with the session's current instruction stack.

        Direct response, not logged — turn-time snapshots already land in
        the event log via the steering-reload push (TD-509).  The session's
        touched paths (TD-503) are forwarded so path-scoped rules report
        their true active/inactive verdict instead of assembling active
        unconditionally.
        """
        found = self.session_registry.get(msg.session_id)
        if found is None:
            return build_error(
                "session_not_found",
                f"Session {msg.session_id!r} not found",
            )
        tier = found.router.active_tier if found.router is not None else "brain"
        assembled = await PromptAssembler(found.workspace_path).assemble(
            tier,
            matched_paths=set(found.touched_paths),
            approved_imports=_approved_import_allowlist(found.workspace_path),
        )
        cached = (
            found.cost_tracker.last_cached_prompt_tokens if found.cost_tracker is not None else None
        )
        return build_instruction_stack(
            found.id,
            assembled.steering,
            seq=1,
            last_cached_tokens=cached,
        ).model_dump_json()

    async def _handle_detach(self, msg: Detach, connection: Any) -> str | None:
        """Handle a detach: stop streaming without affecting the session."""
        session_id = msg.session_id
        conn_key = (id(connection), session_id)

        self._cleanup_attach(connection, session_id, conn_key)
        return None

    def _cleanup_attach(self, connection: Any, session_id: str, conn_key: tuple[int, str]) -> None:
        """Remove connection from attached clients and cancel its stream."""
        # Remove from attached clients
        clients = self._attached_clients.get(session_id)
        if clients:
            clients.discard(connection)
            if not clients:
                del self._attached_clients[session_id]

        # Cancel the streaming task
        task = self._streaming_tasks.pop(conn_key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _on_connection_closed(self, connection: Any) -> None:
        """Clean up all subscriptions for a disconnected client."""
        # Clean up any attach subscriptions for this connection
        for session_id in list(self._attached_clients.keys()):
            conn_key = (id(connection), session_id)
            self._cleanup_attach(connection, session_id, conn_key)

    def health(self) -> dict[str, Any]:
        """Return a health/diagnostics snapshot."""
        return {
            "version": self._version(),
            "uptime": self.state.uptime,
            "active_sessions": self.state.active_sessions,
            "data_dir": str(self.data_dir),
            "shutdown_requested": self._shutdown_event.is_set(),
            "ws_port": self.ws_server.port,
            "ws_clients": self.ws_server.client_count,
        }


def main() -> None:
    """CLI entry point: run the daemon until interrupted."""
    parser = argparse.ArgumentParser(description="TST Desk daemon")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Log level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument("--data-dir", default=None, help="Override the user data directory")
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="Host process PID to watch: if it exits, the daemon shuts down "
        "instead of becoming an orphan (set by the Tauri host).",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    setup_logging(level=args.log_level)

    async def _run() -> None:
        daemon = Daemon(data_dir=data_dir, parent_pid=args.parent_pid)
        await daemon.run()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
