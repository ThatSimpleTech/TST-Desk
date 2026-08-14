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

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { ProtocolClient, type ConnectionState, type SocketLike } from "./client";
import { bindClient, ingestEvent, resetSession } from "./session-status.svelte.js";

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

// Synchronous event fan-out (TD-1004). Consumers that append to a log (chat,
// timeline) cannot ride `lastEvent` — two events inside one effect flush
// would drop the first. Handlers run in the client's sink, in receive order.
const eventSubs = new Set<(event: DaemonEventUnion) => void>();

/** Subscribe to every validated daemon event. Returns an unsubscribe fn. */
export function onDaemonEvent(handler: (event: DaemonEventUnion) => void): () => void {
  eventSubs.add(handler);
  return () => {
    eventSubs.delete(handler);
  };
}

/** Alias kept for the activity timeline lane (TD-1005/1007). */
export const onEvent = onDaemonEvent;

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

function makeClient(): void {
  client = new ProtocolClient(
    {
      async getDaemonInfo() {
        const info = await invoke<{ port: number; token: string } | null>("get_daemon_info");
        return info;
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
      },
      onStateChange(state) {
        ws.state = state;
      },
    },
  );
  bindClient(client);
}

/** Bring the daemon connection up and start following host supervision events. */
export async function connect(): Promise<void> {
  if (unlistenDaemon === null) {
    unlistenDaemon = await listen<DaemonStatus>("daemon-status", (ev) => {
      const payload = ev.payload;
      daemon.state = payload.state;
      daemon.port = payload.port;
      daemon.restart = payload.restart;
    });
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
  unlistenDaemon?.();
  unlistenDaemon = null;
  ws.state = "stopped";
}