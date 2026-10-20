// Typed WebSocket client for the TST Desk daemon (TD-1003).
//
// Talks the daemon protocol (mirrored in `protocol.ts`) over WebSocket
// (loopback, or a Tailscale address from a browser — TD-3701).
// Responsibilities:
//   - hello/hello_ack handshake, then forward sequenced daemon events
//   - automatic reconnect with exponential backoff
//   - re-attach with `from_seq` on reconnect to replay missed events
//   - gap/duplicate detection on the per-session event sequence
//   - tolerant handling of unknown event types (dropped and counted, never
//     dispatched — see the trust-boundary note in handleMessage)
//
// The WebSocket constructor is injected so the client runs in a test under
// vitest's node environment with a fake transport, and in the webview with
// the browser's WebSocket. No Tauri APIs are imported here.

import type { ClientMessageUnion, DaemonEventUnion, HelloAck } from "./protocol";
import { daemonWsUrl } from "./remote-connect";

/**
 * Replies the daemon answers on one connection with seq fixed at 1. They are
 * not in any session's log, so they never touch the replay cursor:
 * design_hit (TD-3403), transcript (TD-4701), artifact_list and artifact
 * (TD-3201), artifact_chunk (TD-5143) and files_restore_preview (TD-5125).
 */
const CONNECTION_SCOPED: ReadonlySet<string> = new Set([
  "design_hit",
  "transcript",
  "artifact_list",
  "artifact",
  "artifact_chunk",
  "files_restore_preview",
]);

/**
 * The daemon event types this client version understands. Messages whose
 * `type` is not here are still seq-advancing but are dropped with a warning.
 * TypeScript cannot enumerate a union's literal members at runtime, so this
 * mirrors `DaemonEventUnion` explicitly and must be kept in sync with it.
 */
const KNOWN_EVENT_TYPES = new Set([
  "ready",
  "session_state",
  "conversation_reset",
  "files_restore_preview", // TD-5125
  "files_restored", // TD-5125
  "user_turn",
  "queued_messages",
  "assistant_delta",
  "assistant_reasoning", // TD-1901
  "assistant_retract", // TD-5159
  "tool_call",
  "tool_result",
  "shell_output",
  "tool_progress", // TD-5105
  "approval_request",
  "question_request",
  "question_answered",
  "decision_logged",
  "checkpoint_notice",
  "verify_started", // TD-5158
  "verify_result",
  "cost_update",
  "boundary_update",
  "turn_complete",
  "tier_state",
  "context_compacted",
  "context_usage",
  "task_list", // TD-5122
  "steering_reloaded",
  "rule_activated",
  "tier_switched",
  "model_fallback", // TD-5120
  "instruction_stack",
  "instruction_files",
  "command_list",
  "context_pins",
  "memory_files",
  "charter",
  "autonomy_start",
  "autonomy_summary", // TD-4303
  "memory_proposal",
  "steering_proposal", // TD-5140
  "steering_decision", // TD-5140
  "session_list",
  "policy_rules", // TD-803: was missing; settings events arrived as unknown
  "setup_state", // TD-1101 first-run wizard
  "pairing_code", // TD-5014
  "pairing_completed", // TD-5014
  "machine_list", // TD-5015
  "api_key_validated", // TD-1101
  "diagnostics_report", // TD-1104 doctor
  "usage_report", // TD-1706: was missing; the usage panel never loaded
  "usage_exported", // TD-1706
  "log_trimmed", // TD-2901: attach from a rotated seq
  "artifact_ready", // TD-3201
  "artifact_list", // TD-3201
  "artifact", // TD-3201
  "artifact_chunk", // TD-5143
  "error",
  "screen_frame", // TD-1710
  "cu_kill_state", // TD-3402 / TD-3404
  "cu_session", // TD-3407: Screen-pane glow; was missing and the gate dropped it
  "focus_window", // TD-4832: raise the window when the last CU episode closes
  "design_hit", // TD-3403
  "cu_permissions", // TD-3302
  "job_list", // TD-3805
  "job_runs", // TD-3811
  "job_draft", // TD-3803
  "job_templates", // TD-3816
  "session_job_source", // TD-3816
  "grok_commands",
  "grok_plan",
  "grok_mode",
  "grok_preview",
  "grok_session_list",
  "grok_extensions",
  "transcript", // TD-4701
  "email_test_result", // TD-3820
  "github_status", // TD-4852
  "microsoft_status", // TD-4853
  "google_status", // TD-4854
]);

/**
 * Minimal surface of a WebSocket the client depends on.
 *
 * Handler fields are writable and accept an optional event argument so both
 * the browser `WebSocket` (whose callbacks receive an `Event`) and a test fake
 * (invoked with none) satisfy the type.
 */
export interface SocketLike {
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
}

export type SocketFactory = (url: string) => SocketLike;

export type ConnectionState = "disconnected" | "connecting" | "connected" | "reconnecting" | "stopped";

/**
 * How long a socket the client still believes is connected may go without a
 * single frame — daemon event or `ping` — before a resume calls it a zombie
 * (TD-1716).
 *
 * The daemon pings every ~15s, so this is two missed frames: one late ping is
 * not a verdict, but a webview that slept through a suspension has missed far
 * more than two. The check only runs on resume; a quiet foreground app is
 * never judged by it.
 */
export const ZOMBIE_SILENCE_MS = 30_000;

/** A handler receiving a parsed, validated daemon event. */
export type EventHandler = (event: DaemonEventUnion) => void;

/** Live daemon address. `host` is set for a browser attach (TD-3701); omitted is loopback. */
export type DaemonInfo = { port: number; token: string; host?: string };

/** Gets the live daemon {port, token, host?}. Injected so tests supply a stub. */
export type DaemonInfoProvider = () => Promise<DaemonInfo | null>;

export interface ClientOptions {
  /** Resolves the daemon's port + auth token. */
  getDaemonInfo: DaemonInfoProvider;
  /** Builds the ws:// URL. Defaults to `host` or 127.0.0.1. */
  buildUrl?: (port: number) => string;
  /** Injectable WebSocket constructor (browser WebSocket in production). */
  socketFactory: SocketFactory;
  /** Base backoff in ms; clamped by `maxBackoffMs`. Default 500. */
  baseBackoffMs?: number;
  /** Upper bound on backoff. Default 15s. */
  maxBackoffMs?: number;
  /** Protocol version sent in `hello`. Must match core PROTOCOL_VERSION. */
  protocolVersion?: number;
  /** `hello.features` opt-ins. Default `HELLO_FEATURES`. */
  features?: readonly string[];
}

/** What this UI opts into on hello (TD-5015): `"machines"` makes the
 *  connection a viewer, so the daemon merges its paired computers' rows into
 *  session_list and sends machine_list. An older daemon ignores it. */
export const HELLO_FEATURES: readonly string[] = ["machines"];

/** Sink for all events including connection-state transitions. */
export interface ClientSink {
  /** Invoked with each validated daemon event, in received order. */
  onEvent?: EventHandler;
  /** Invoked whenever the connection state changes. */
  onStateChange?: (state: ConnectionState) => void;
  /** Invoked when an unknown event type is dropped. */
  onUnknownEvent?: (type: string) => void;
  /** Invoked with each `hello_ack`, before the state turns "connected", so
   *  the daemon's identity (TD-5015) is known to whoever reacts to that. */
  onHandshake?: (ack: HelloAck) => void;
}

export class ProtocolClient {
  private readonly opts: ClientOptions;
  private readonly sink: ClientSink;
  private socket: SocketLike | null = null;
  private state: ConnectionState = "disconnected";
  private stopped = false;
  private reconnectAttempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private handshake: "idle" | "awaiting_ack" = "idle";
  // session_id -> last event seq we accepted from the daemon.
  private readonly lastSeqBySession = new Map<string, number>();
  // session_id of every session this connection is (or should be) attached to.
  private readonly attachedSessions = new Set<string>();
  // True after the first handshake; distinguishes first connect from a reconnect.
  private hasConnectedOnce = false;
  // Frames dispatch() dropped because their type is not in KNOWN_EVENT_TYPES.
  // Read-only after construction, through the `unknownEventCount` getter.
  private unknownEventsDropped = 0;
  // Epoch ms of the last frame this socket delivered — any frame, event or
  // ping. Read only by `resume()` (TD-1716); 0 until the socket opens.
  private lastFrameAt = 0;
  // Bumped by every open() and by stop(): an open() whose daemon-info await
  // was overtaken by either must not create a socket (TD-5015).
  private openGeneration = 0;

  constructor(opts: ClientOptions, sink: ClientSink = {}) {
    this.opts = opts;
    this.sink = sink;
  }

  /** Current connection state. */
  get connectionState(): ConnectionState {
    return this.state;
  }

  /** Highest accepted seq for a session (or 0 if unattached). */
  lastSeq(sessionId: string): number {
    return this.lastSeqBySession.get(sessionId) ?? 0;
  }

  /** How many frames were dropped because their `type` is outside the union. */
  get unknownEventCount(): number {
    return this.unknownEventsDropped;
  }

  /**
   * Attach to a session. On the live socket this sends `attach{from_seq}` so
   * the daemon replays from our last seen seq and then streams live. On a
   * reconnect the client re-attaches every registered session itself.
   */
  attach(sessionId: string): void {
    const fromSeq = this.lastSeq(sessionId) + 1;
    this.attachedSessions.add(sessionId);
    this.sendAttach(sessionId, fromSeq);
  }

  /** Stop following a session. */
  detach(sessionId: string): void {
    this.attachedSessions.delete(sessionId);
    this.lastSeqBySession.delete(sessionId);
    this.send({ type: "detach", session_id: sessionId });
  }

  /**
   * Send a client→daemon message. Returns false (and sends nothing) unless
   * the socket is open and handshaken, so callers can decide whether an
   * optimistic local update is warranted (TD-1007).
   *
   * Guarding on `state === "connected"` is the unambiguous handshake signal:
   * `handshake` is "idle" both before the socket opens and after the ack,
   * and it stays "idle" across a reconnect while `this.socket` still points
   * at the closed socket — so a `handshake`-only guard would throw on a dead
   * socket mid-reconnect.
   */
  send(msg: ClientMessageUnion): boolean {
    if (!this.socket || this.state !== "connected") return false;
    // Attach-before-send invariant (TD-1713): the daemon fans events out only
    // to attached connections, so a user_message sent unattached is consumed
    // with nothing ever streaming back — the silent stall of 2026-08-14.
    // Attach on the same socket first; the daemon processes frames in order,
    // so the attach (and its replay) lands before the message is enqueued.
    if (msg.type === "user_message" && !this.attachedSessions.has(msg.session_id)) {
      this.attach(msg.session_id);
    }
    this.socket.send(JSON.stringify(msg));
    return true;
  }

  /** Open a workspace directory; the daemon answers with session_state. */
  openWorkspace(path: string): void {
    this.send({ type: "open_workspace", path });
  }

  /** Create a fresh session in an existing session's workspace (TD-1701).
   *  The daemon answers with the new session's first event (session_state). */
  newSession(anchorSessionId: string): boolean {
    return this.send({ type: "new_session", session_id: anchorSessionId });
  }

  /** Pin a session's model tier (TD-1006). The daemon acks with tier_state. */
  setTier(sessionId: string, tier: "brain" | "worker" | "validator"): void {
    this.send({ type: "set_tier", session_id: sessionId, tier });
  }

  /** Turn plan mode (brain lock) on or off (TD-4603). Acked with tier_state. */
  setPlan(sessionId: string, on: boolean): void {
    this.send({ type: "set_plan", session_id: sessionId, on });
  }

  /** Retarget this session at a catalog preset (TD-1721). Acked with session_list. */
  setSessionPreset(sessionId: string, name: string): void {
    this.send({ type: "set_session_preset", session_id: sessionId, name });
  }

  /** Confirm a pending interactive verify (TD-4204 ask mode). */
  runVerify(sessionId: string): boolean {
    return this.send({ type: "run_verify", session_id: sessionId });
  }

  /** Skip a pending interactive verify (TD-4204 ask mode). */
  denyVerify(sessionId: string): boolean {
    return this.send({ type: "deny_verify", session_id: sessionId });
  }

  // ── Onboarding (TD-1101 first-run wizard) ───────────────────────────

  /** Ask for the setup state (key presence, presets). Replies with setup_state. */
  getSetupState(): void {
    this.send({ type: "get_setup_state" });
  }

  /** Store an API key in the OS keychain. Acked with setup_state. */
  setApiKey(apiKey: string): void {
    this.send({ type: "set_api_key", api_key: apiKey });
  }

  /** Probe the stored key with one cheap live call. Replies api_key_validated. */
  validateApiKey(): void {
    this.send({ type: "validate_api_key" });
  }

  /** Choose the active model preset. Acked with setup_state. */
  setPreset(name: string): void {
    this.send({ type: "set_preset", name });
  }

  /** Create or replace a listed MCP server. Acked with setup_state. */
  setMcpServer(server: {
    id: string;
    transport: "stdio" | "http";
    command?: string[];
    url?: string;
    enabled?: boolean;
  }): void {
    this.send({
      type: "set_mcp_server",
      id: server.id,
      transport: server.transport,
      command: server.command ?? [],
      url: server.url ?? "",
      enabled: server.enabled ?? true,
    });
  }

  /** Remove a listed MCP server. Acked with setup_state. */
  deleteMcpServer(id: string): void {
    this.send({ type: "delete_mcp_server", id });
  }

  // ── Diagnostics (TD-1104 doctor) ────────────────────────────────────

  /** Run the doctor checks; the daemon replies with diagnostics_report. */
  runDiagnostics(): void {
    this.send({ type: "run_diagnostics" });
  }

  /** Start the client: resolve daemon info and open the first connection. */
  async start(): Promise<void> {
    this.stopped = false;
    await this.open();
  }

  /**
   * Heal after the window came back (TD-1716).
   *
   * macOS suspends an occluded WKWebView's JavaScript: timers stop firing and
   * socket frames queue at the OS while the socket itself stays open and
   * healthy. Nothing in here notices — which is why the cure is applied on the
   * way back rather than defended against. We make no attempt to work out what
   * was missed: every followed session is re-attached at `lastSeq + 1` and the
   * daemon's replay closes whatever gap the suspension left, losslessly.
   *
   * A socket that produced no frame at all — not even a ping — for longer than
   * `ZOMBIE_SILENCE_MS` is dead however healthy the transport claims it is, so
   * re-attaching into it would be shouting down a hole. Force it closed and let
   * the existing reconnect path do the re-attach on its `hello_ack`.
   */
  resume(): void {
    if (this.stopped || this.state !== "connected") return;

    if (Date.now() - this.lastFrameAt > ZOMBIE_SILENCE_MS) {
      console.warn("[tstd client] no frame since suspension; treating the socket as a zombie");
      this.reconnectAttempt = 0; // reconnect now, not on a backoff
      this.forceReconnect();
      return;
    }

    for (const sessionId of this.attachedSessions) {
      this.sendAttach(sessionId, this.lastSeq(sessionId) + 1);
    }
  }

  private sendAttach(sessionId: string, fromSeq: number): void {
    // Only meaningful on an open, handshaken socket.
    this.send({ type: "attach", session_id: sessionId, from_seq: fromSeq });
  }

  /** Permanently stop: close the socket, cancel retries, mark stopped. */
  stop(): void {
    this.stopped = true;
    this.openGeneration += 1;
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.handshake = "idle";
    this.lastSeqBySession.clear();
    this.socket?.close();
    this.socket = null;
    this.setState("stopped");
  }

  private async open(): Promise<void> {
    if (this.stopped) return;
    const generation = ++this.openGeneration;
    this.setState(this.reconnectAttempt === 0 ? "connecting" : "reconnecting");

    let info: DaemonInfo | null;
    try {
      info = await this.opts.getDaemonInfo();
    } catch {
      info = null;
    }
    // A stop(), or a newer open(), that landed during the await owns the
    // connection now; opening here would leave a second live socket that
    // flips the state back to "connected" after stop (TD-5015).
    if (this.stopped || generation !== this.openGeneration) return;
    if (!info) {
      this.scheduleRetry();
      return;
    }

    const url = this.opts.buildUrl?.(info.port) ?? daemonWsUrl(info.host ?? "127.0.0.1", info.port);
    const socket = this.opts.socketFactory(url);
    const previous = this.socket;
    this.socket = socket;
    // Disowned first, so its onclose below returns without a retry.
    previous?.close();
    const features = [...(this.opts.features ?? HELLO_FEATURES)];

    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.lastFrameAt = Date.now();
      // hello carries the auth token; the daemon replies hello_ack then ready.
      socket.send(
        JSON.stringify({ type: "hello", token: info!.token, version: this.opts.protocolVersion ?? 1, features }),
      );
      this.handshake = "awaiting_ack";
    };

    // A socket this client no longer owns may still deliver frames; none of
    // them may reach a store or move a seq cursor.
    socket.onmessage = (ev) => {
      if (this.socket !== socket) return;
      this.handleMessage(String(ev.data));
    };

    socket.onclose = () => {
      // A socket we already replaced (forceReconnect) closing later must not
      // schedule a second reconnect on top of the one in flight.
      if (this.socket !== socket) return;
      this.handshake = "idle";
      if (this.stopped) return;
      this.setState("reconnecting");
      this.scheduleRetry();
    };

    // `onerror` alone means nothing — close follows. Keep it wired to avoid
    // an unhandled event and to make the transport's failure observable.
    socket.onerror = () => {};
  }

  private handleMessage(raw: string): void {
    // Any frame at all proves this page's JavaScript is running, which is the
    // only thing `resume()`'s zombie test asks of it — so stamp before
    // parsing, malformed frames included (TD-1716).
    this.lastFrameAt = Date.now();

    let msg: unknown;
    try {
      msg = JSON.parse(raw);
    } catch {
      // Not JSON — not our protocol's shape; ignore without crashing.
      console.warn("[tstd client] dropped non-JSON message");
      return;
    }
    if (typeof msg !== "object" || msg === null || !("type" in msg) || typeof msg.type !== "string") {
      console.warn("[tstd client] dropped malformed message");
      return;
    }

    // Trust boundary (TD-4816): this is the whole check. We have verified only
    // that the frame is a JSON object naming a string `type`; every payload
    // field below is cast from `protocol.ts`, not validated. That residual
    // trust is placed in the socket's peer — the daemon we launched, reached
    // over loopback or a token-authenticated attach — because both ends of the
    // wire ship from this repo and the TS mirror is the shared contract. What
    // IS enforced: a frame whose type is not in KNOWN_EVENT_TYPES never
    // reaches a store; it is dropped and counted (see dispatch()).

    const type = msg.type as string;

    // Out-of-band handshake reply: no seq. If we previously held a connection
    // we re-attach every known session at lastSeq+1 so the daemon replays
    // anything we missed while offline.
    if (type === "hello_ack") {
      this.handshake = "idle";
      const reconnecting = this.hasConnectedOnce;
      this.hasConnectedOnce = true;
      this.reconnectAttempt = 0;
      this.sink.onHandshake?.(msg as HelloAck);
      // Mark connected before re-attaching: `send` guards on the connected
      // state, and the re-attach below must pass that guard (see send()).
      this.setState("connected");
      if (reconnecting) {
        for (const sessionId of this.attachedSessions) {
          this.sendAttach(sessionId, this.lastSeq(sessionId) + 1);
        }
      }
      return;
    }

    // The daemon's liveness frame (TD-1716). Out-of-band like hello_ack: it
    // belongs to no session's log, carries no seq, and says nothing a store
    // could reduce. Its whole payload was the timestamp taken above.
    if (type === "ping") return;

    // Sequenced daemon events carry `seq` and (for session-scoped events) a
    // session_id. Accept returns false for a duplicate or an out-of-order
    // (gapped) seq; those are dropped and must not reach the sink.
    const seq = typeof (msg as Record<string, unknown>).seq === "number" ? ((msg as Record<string, unknown>).seq as number) : undefined;
    const sessionId = (msg as Record<string, unknown>).session_id as string | undefined;

    // TD-1204: get_instruction_stack answers with a snapshot stamped
    // seq=1 that is not in the session log. After attach has advanced
    // lastSeq, acceptSequenced would drop it as a stale duplicate — which
    // is why the Stack tab stayed on "No instruction stack yet." A live
    // TD-509 push *is* in the log and arrives as last+1; still advance
    // the cursor then, so the next logged event is not a false gap.
    if (type === "instruction_stack") {
      if (sessionId !== undefined && seq !== undefined && seq === this.lastSeq(sessionId) + 1) {
        this.lastSeqBySession.set(sessionId, seq);
      }
      this.dispatch(msg as DaemonEventUnion);
      return;
    }

    // Connection-scoped replies (seq=1, not in the log). acceptSequenced
    // would drop them after attach the way it dropped instruction_stack
    // snapshots before TD-1204, or take seq 1 as the log's first event
    // before attach and then drop the real one as a duplicate.
    if (CONNECTION_SCOPED.has(type)) {
      this.dispatch(msg as DaemonEventUnion);
      return;
    }

    // TD-2901: the durable window dropped seqs before earliest_seq.
    // Jump the cursor so replay from the kept prefix is not a false gap,
    // and do not treat the connection-scoped seq=1 as a session event.
    if (type === "log_trimmed") {
      const earliest = (msg as Record<string, unknown>).earliest_seq;
      if (typeof sessionId === "string" && typeof earliest === "number" && earliest > 1) {
        const jumped = earliest - 1;
        if (jumped > this.lastSeq(sessionId)) {
          this.lastSeqBySession.set(sessionId, jumped);
        }
      }
      this.dispatch(msg as DaemonEventUnion);
      return;
    }

    if (seq !== undefined) {
      if (sessionId !== undefined && !this.acceptSequenced(sessionId, seq)) {
        return;
      }
    }

    this.dispatch(msg as DaemonEventUnion);
  }

  /**
   * Sequence bookkeeping: accept an event iff it advances the session's log
   * by exactly one. Anything else is handled per the no-gap/no-dup contract:
   *   - seq <= lastSeq  -> duplicate already seen, drop.
   *   - seq > lastSeq+1 -> we missed events; re-attach with from_seq=last+1
   *     so the daemon replays the gap, then we reconnect for a fresh stream.
   */
  private acceptSequenced(sessionId: string, seq: number): boolean {
    const last = this.lastSeq(sessionId);
    if (seq <= last) {
      console.warn(`[tstd client] dropped duplicate/old seq ${seq} for ${sessionId} (last ${last})`);
      return false; // do not forward — already seen.
    }
    if (seq > last + 1) {
      console.warn(`[tstd client] gap detected for ${sessionId}: expected ${last + 1}, got ${seq}; re-attaching`);
      this.reconnectAttempt = 0; // force an immediate, fresh attach
      void this.forceReconnect();
      return false; // do not forward the out-of-order event.
    }
    this.lastSeqBySession.set(sessionId, seq);
    return true;
  }

  /**
   * Dispatch a validated event to the sink. Unknown event types (those not in
   * the typed union) must never crash the client — drop and count them. Their
   * seq still advanced above, so gap detection won't falsely fire afterwards.
   */
  private dispatch(msg: DaemonEventUnion): void {
    // The daemon may emit future/unknown types the TS mirror doesn't know.
    // TypeScript casts here can't know the full future shape; runtime guard is
    // the real tolerance. Unknown types are dropped with a warning and counted,
    // so a drift between the daemon's union and this mirror is observable.
    const type = (msg as { type?: string }).type;
    if (type === undefined) {
      console.warn(`[tstd client] dropped message without a type`);
      return;
    }
    const known = KNOWN_EVENT_TYPES.has(type);
    if (!known) {
      this.unknownEventsDropped += 1;
      console.warn(`[tstd client] ignored unknown event type "${type}"`);
      this.sink.onUnknownEvent?.(type);
      return;
    }
    this.sink.onEvent?.(msg);
  }

  private forceReconnect(): void {
    if (this.stopped) return;
    // Disown before closing, not after: `close()` can deliver `onclose`
    // synchronously (and does on a zombie socket we closed ourselves), and a
    // socket we have already replaced must not schedule a retry on top of the
    // reconnect this call is about to start.
    const dead = this.socket;
    this.socket = null;
    dead?.close();
    void this.open();
  }

  private scheduleRetry(): void {
    if (this.stopped) return;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    const attempt = this.reconnectAttempt;
    // `??` binds looser than `*`: without the parens a configured base is
    // applied flat and only the default grows exponentially (TD-4801).
    const backoff = Math.min((this.opts.baseBackoffMs ?? 500) * 2 ** attempt, this.opts.maxBackoffMs ?? 15_000);
    this.reconnectAttempt += 1;
    this.retryTimer = setTimeout(() => void this.open(), backoff);
  }

  private setState(state: ConnectionState): void {
    if (this.state === state) return;
    this.state = state;
    this.sink.onStateChange?.(state);
  }
}