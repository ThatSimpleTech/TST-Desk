// Connection-state store (TD-1003).
//
// Owns the ProtocolClient and surfaces two reactive values:
//   - `ws` — the client's own connection state (disconnected/connecting/
//     connected/reconnecting/stopped) derived from actual socket events.
//   - `daemon` — the supervisor's reported lifecycle state (starting,
//     connected, crashed, stopping, stopped) from the host's `daemon-status`
//     Tauri event, plus the live port/restart count.
//
// The UI never infers daemon state on its own (AGENTS §6): "daemon" is read
// only from the host event; "ws" is read only from the real socket. We expose
// both so the banner can say, e.g., "daemon crashed — reconnecting".
//
// A browser attach (TD-3701) has no host supervisor. There we mirror `ws`
// into `daemon` so the banner does not treat a missing Tauri event as
// "Couldn't start the daemon".

import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { ProtocolClient, type ConnectionState, type DaemonInfo, type SocketLike } from "./client";
import { bindClient, ingestEvent, resetSession } from "./session-status.svelte.js";
import { clearNotifications, notifyEvent } from "./notifications.svelte.js";
import { watchResume } from "./resume";
import { isTauri } from "./open-file";
import {
  parseStoredAttach,
  resolveBrowserTarget,
  serializeAttach,
  stripAttachFromUrl,
  validateAttachTarget,
  type AttachTarget,
} from "./remote-connect";

type UnlistenFn = () => void;

const REMOTE_ATTACH_STORAGE = "tstdesk.remoteAttach";

export interface DaemonStatus {
  state: "starting" | "connected" | "crashed" | "stopping" | "stopped";
  port: number | null;
  restart: number;
}

let unlistenDaemon: UnlistenFn | null = null;
let client: ProtocolClient | null = null;

export const ws = $state<{ state: ConnectionState }>({ state: "disconnected" });
export const daemon = $state<DaemonStatus>({ state: "stopped", port: null, restart: 0 });
// Latest validated daemon event, for subscribers that want the stream.
export const lastEvent = $state<{ event: DaemonEventUnion | null }>({ event: null });

/** Browser-only: the connect form is up because no ws+token is known yet. */
export const remoteAttach = $state<{ needed: boolean; error: string | null }>({
  needed: false,
  error: null,
});

let remoteTarget: AttachTarget | null = null;

// Synchronous event fan-out (TD-1004). Consumers that append to a log (chat,
// timeline) cannot ride `lastEvent` — two events inside one effect flush
// would drop the first. Handlers run in the client's sink, in receive order.
const eventSubs = new Set<(event: DaemonEventUnion) => void>();

// Connection-state fan-out (TD-1101): the onboarding wizard needs the
// handshake-complete transition itself (no daemon event marks it) so it can
// probe setup state immediately, not on the next keystroke.
const stateSubs = new Set<(state: ConnectionState) => void>();

// Resume fan-out (TD-1716): the webview can be suspended mid-turn, so stores
// holding a wait need the same "you were asleep" signal the socket gets.
const resumeSubs = new Set<() => void>();
let stopResumeWatch: (() => void) | null = null;

/** Subscribe to every validated daemon event. Returns an unsubscribe fn. */
export function onDaemonEvent(handler: (event: DaemonEventUnion) => void): () => void {
  eventSubs.add(handler);
  return () => {
    eventSubs.delete(handler);
  };
}

/** Subscribe to connection-state transitions. Returns an unsubscribe fn. */
export function onConnectionState(handler: (state: ConnectionState) => void): () => void {
  stateSubs.add(handler);
  return () => {
    stateSubs.delete(handler);
  };
}

/** Subscribe to resume — the page coming back from suspension (TD-1716).
 *  Returns an unsubscribe fn. */
export function onResume(handler: () => void): () => void {
  resumeSubs.add(handler);
  return () => {
    resumeSubs.delete(handler);
  };
}

/** Alias kept for the activity timeline lane (TD-1005/1007). */
export const onEvent = onDaemonEvent;

/** Heal the connection first — its re-attach frames go out on this tick and
 *  the daemon's replay is already in flight — then let the stores re-decide
 *  what they were waiting on. */
function handleResume(): void {
  client?.resume();
  for (const sub of resumeSubs) sub();
}

/** Send a client→daemon message. False when no handshaken socket exists. */
export function sendToDaemon(msg: ClientMessageUnion): boolean {
  return client?.send(msg) ?? false;
}

/** Attach/detach a session's event stream on the shared connection. */
export function attachToSession(sessionId: string): void {
  client?.attach(sessionId);
}

export function detachFromSession(sessionId: string): void {
  client?.detach(sessionId);
}

function targetToInfo(target: AttachTarget): DaemonInfo {
  return { host: target.host, port: target.port, token: target.token };
}

function persistRemoteTarget(target: AttachTarget): void {
  if (typeof sessionStorage === "undefined") return;
  try {
    sessionStorage.setItem(REMOTE_ATTACH_STORAGE, serializeAttach(target));
  } catch {
    // Quota or private mode — reconnect this tab still works via remoteTarget.
  }
}

function loadStoredTarget(): AttachTarget | null {
  if (typeof sessionStorage === "undefined") return null;
  try {
    return parseStoredAttach(sessionStorage.getItem(REMOTE_ATTACH_STORAGE));
  } catch {
    return null;
  }
}

function applyBrowserTarget(): boolean {
  if (typeof window === "undefined") return false;
  const resolved = resolveBrowserTarget({
    search: window.location.search,
    hash: window.location.hash,
    stored: loadStoredTarget(),
  });
  if (!resolved.ok) {
    remoteTarget = null;
    remoteAttach.needed = true;
    remoteAttach.error = resolved.error;
    // Not a host failure — the user has not attached yet.
    daemon.state = "starting";
    return false;
  }
  remoteTarget = resolved.target;
  remoteAttach.needed = false;
  remoteAttach.error = null;
  persistRemoteTarget(resolved.target);
  if (resolved.stripUrl) {
    window.history.replaceState(null, "", stripAttachFromUrl(window.location.href));
  }
  daemon.state = "starting";
  daemon.port = resolved.target.port;
  return true;
}

function syncRemoteDaemonMirror(state: ConnectionState): void {
  if (isTauri()) return;
  if (state === "connected") {
    daemon.state = "connected";
    daemon.port = remoteTarget?.port ?? daemon.port;
    return;
  }
  if (state === "stopped") {
    daemon.state = "stopped";
    return;
  }
  daemon.state = "starting";
}

function makeClient(): void {
  client = new ProtocolClient(
    {
      async getDaemonInfo() {
        if (isTauri()) {
          const { invoke } = await import("@tauri-apps/api/core");
          return invoke<DaemonInfo | null>("get_daemon_info");
        }
        return remoteTarget === null ? null : targetToInfo(remoteTarget);
      },
      socketFactory(url) {
        // The browser WebSocket's handlers are typed with `this: WebSocket`
        // and an `Event` param; SocketLike intentionally narrows them so a
        // test fake can implement the same shape. The browser socket is
        // behaviorally identical, so cast once at this boundary.
        return new WebSocket(url) as SocketLike;
      },
    },
    {
      onEvent(event) {
        lastEvent.event = event;
        for (const sub of eventSubs) sub(event);
        ingestEvent(event); // wire the session reducer (TD-1006)
        notifyEvent(event); // turn failures / pauses / errors into notices (TD-1008)
      },
      onStateChange(state) {
        ws.state = state;
        syncRemoteDaemonMirror(state);
        for (const sub of stateSubs) sub(state);
      },
    },
  );
  bindClient(client);
}

/** Browser form submit (TD-3701). Starts the same ProtocolClient once a target is valid. */
export function connectRemote(wsUrl: string, token: string): boolean {
  const parsed = validateAttachTarget(wsUrl, token);
  if (!parsed.ok) {
    remoteAttach.needed = true;
    remoteAttach.error = parsed.reason === "invalid" ? parsed.error : "Enter a WebSocket URL and token.";
    return false;
  }
  remoteTarget = parsed.target;
  remoteAttach.needed = false;
  remoteAttach.error = null;
  persistRemoteTarget(parsed.target);
  daemon.state = "starting";
  daemon.port = parsed.target.port;
  void connect();
  return true;
}

/** Bring the daemon connection up and start following host supervision events. */
export async function connect(): Promise<void> {
  if (isTauri()) {
    if (unlistenDaemon === null) {
      const { listen } = await import("@tauri-apps/api/event");
      unlistenDaemon = await listen<DaemonStatus>("daemon-status", (ev) => {
        const payload = ev.payload;
        daemon.state = payload.state;
        daemon.port = payload.port;
        daemon.restart = payload.restart;
      });
    }
  } else if (remoteTarget === null && !applyBrowserTarget()) {
    return;
  }
  if (stopResumeWatch === null && typeof document !== "undefined" && typeof window !== "undefined") {
    stopResumeWatch = watchResume(document, window, handleResume);
  }
  if (client === null) makeClient();
  await client!.start();
}

/** Tear down the client and its subscriptions. */
export function disconnect(): void {
  client?.stop();
  client = null;
  bindClient(null);
  resetSession();
  clearNotifications();
  unlistenDaemon?.();
  unlistenDaemon = null;
  stopResumeWatch?.();
  stopResumeWatch = null;
  remoteTarget = null;
  remoteAttach.needed = false;
  remoteAttach.error = null;
  ws.state = "stopped";
}