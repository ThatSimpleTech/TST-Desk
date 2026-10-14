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
import { attachStore } from "./attach-store";
import {
  daemonWsUrl,
  parseStoredAddress,
  parseStoredAttach,
  resolveBrowserTarget,
  serializeAddress,
  serializeAttach,
  stripAttachFromUrl,
  validateAttachTarget,
  type AttachTarget,
} from "./remote-connect";
import {
  attachStatusNote,
  bridgeDeviceName,
  canScanPairing,
  COULD_NOT_SCAN,
  installPairingHandlers,
  pairLinkFromTyped,
  parsePairLink,
  readAttachStatus,
  redeemPairing,
  refusedKeyMessage,
  scanFailureMessage,
  startPairingScan,
  takePendingPairLink,
  type PairLinkParse,
  type ScanResult,
} from "./pairing";

type UnlistenFn = () => void;

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
export const remoteAttach = $state<{
  needed: boolean;
  error: string | null;
  /** Address to prefill after a refused token (TD-5005). Never the token. */
  lastUrl: string;
  /** True when this device keeps the target after the app closes. */
  durable: boolean;
  /** The paired computer's name (TD-5014), for the banner and refusal copy. */
  desktopName: string | null;
  /** A pairing code is being redeemed. */
  pairing: boolean;
  /** The shell can open its QR scanner. */
  canScan: boolean;
  /** Why the shell could not open the saved connection, in plain words. */
  storeNote: string | null;
  /** A saved computer that still works, to go back to after a failed re-pair. */
  fallback: string | null;
}>({
  needed: false,
  error: null,
  lastUrl: "",
  durable: false,
  desktopName: null,
  pairing: false,
  canScan: false,
  storeNote: null,
  fallback: null,
});

let remoteTarget: AttachTarget | null = null;
let stopPairingWatch: (() => void) | null = null;

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
  const store = attachStore();
  remoteAttach.durable = store.durable;
  store.save(serializeAttach(target));
}

function loadStoredTarget(): AttachTarget | null {
  return parseStoredAttach(attachStore().load());
}

/** A remote hello was refused: stop retrying and ask to pair again (TD-5005).
 *  TD-5014: the key is dead but the address is not, so keep where and who and
 *  drop only the token. A network failure never reaches here. */
function handleRefusedToken(): void {
  client?.stop();
  const store = attachStore();
  const refused = remoteTarget;
  remoteTarget = null;
  if (refused !== null) {
    const desktopName = refused.desktopName ?? null;
    store.save(serializeAddress({ host: refused.host, port: refused.port, desktopName }));
    remoteAttach.lastUrl = daemonWsUrl(refused.host, refused.port);
    remoteAttach.desktopName = desktopName;
  }
  remoteAttach.needed = true;
  remoteAttach.fallback = null;
  remoteAttach.error = refusedKeyMessage(remoteAttach.desktopName, store.durable);
}

/** Forget the saved computer on this device and show the attach form. */
export function forgetRemoteTarget(): void {
  if (isTauri()) return;
  client?.stop();
  attachStore().clear();
  remoteAttach.lastUrl = remoteTarget === null ? "" : daemonWsUrl(remoteTarget.host, remoteTarget.port);
  remoteTarget = null;
  remoteAttach.needed = true;
  remoteAttach.error = null;
  remoteAttach.desktopName = null;
  remoteAttach.fallback = null;
}

// ── Pairing (TD-5014) ────────────────────────────────────────────────

/** "Scan the code on your computer". The result comes back through the shell. */
export function scanToPair(): void {
  remoteAttach.error = null;
  if (!startPairingScan()) remoteAttach.error = COULD_NOT_SCAN;
}

/** A scanned QR or an opened tstdesk://pair link. */
export function pairFromText(text: string): Promise<boolean> {
  return pairWith(parsePairLink(text));
}

/** "Type the code instead": address + code. */
export function pairFromTyped(address: string, code: string): Promise<boolean> {
  return pairWith(pairLinkFromTyped(address, code));
}

/** Back to the saved computer after a failed re-pair. */
export function useSavedTarget(): void {
  remoteAttach.error = null;
  remoteAttach.fallback = null;
  if (remoteTarget !== null) {
    remoteAttach.needed = false;
    return;
  }
  void connect();
}

async function pairWith(parsed: PairLinkParse): Promise<boolean> {
  if (remoteAttach.pairing) return false;
  if (!parsed.ok) {
    showPairFailure(parsed.error);
    return false;
  }
  const { host, port, code, desktopName } = parsed.link;
  remoteAttach.needed = true;
  remoteAttach.pairing = true;
  remoteAttach.error = null;
  remoteAttach.lastUrl = daemonWsUrl(host, port);
  const result = await redeemPairing({ host, port, code, desktopName, deviceName: bridgeDeviceName() });
  remoteAttach.pairing = false;
  if (!result.ok) {
    // The store is untouched: a wrong code or a dropped network is not a
    // reason to forget a computer that still works.
    showPairFailure(result.message);
    return false;
  }
  usePairedTarget(result.target);
  return true;
}

function showPairFailure(message: string): void {
  remoteAttach.needed = true;
  remoteAttach.error = message;
  const saved = remoteTarget ?? loadStoredTarget();
  remoteAttach.fallback = saved === null ? null : (saved.desktopName ?? saved.host);
}

function usePairedTarget(target: AttachTarget): void {
  const previous = remoteTarget;
  remoteTarget = target;
  persistRemoteTarget(target);
  remoteAttach.needed = false;
  remoteAttach.error = null;
  remoteAttach.fallback = null;
  remoteAttach.storeNote = null;
  remoteAttach.desktopName = target.desktopName ?? null;
  remoteAttach.lastUrl = daemonWsUrl(target.host, target.port);
  // Re-paired with the computer this socket is already on: keep the live
  // connection; the next hello carries the new key.
  const same = previous !== null && previous.host === target.host && previous.port === target.port;
  if (same && ws.state === "connected") return;
  client?.stop();
  daemon.state = "starting";
  daemon.port = target.port;
  void connect();
}

function handleScan(result: ScanResult): void {
  if (result.ok) {
    void pairFromText(result.text);
    return;
  }
  const message = scanFailureMessage(result.reason);
  if (message !== null) remoteAttach.error = message;
}

/** Install the shell's scan / opened-link callbacks once per page. */
function watchPairing(): void {
  remoteAttach.canScan = canScanPairing();
  if (stopPairingWatch !== null) return;
  stopPairingWatch = installPairingHandlers({
    onScan: handleScan,
    onPairLink: (link) => void pairFromText(link),
  });
}

/** True for the daemon's handshake refusal, not a provider's own auth error. */
export function isHandshakeRefusal(event: DaemonEventUnion, state: ConnectionState): boolean {
  return (
    event.type === "error" &&
    event.code === "auth_failed" &&
    (event.session_id ?? null) === null &&
    state !== "connected"
  );
}

function applyBrowserTarget(): boolean {
  if (typeof window === "undefined") return false;
  const store = attachStore();
  const raw = store.load();
  // TD-5014: say plainly when the shell could not open what it saved, so a
  // phone that "forgot" is diagnosable from the form itself.
  remoteAttach.storeNote = attachStatusNote(readAttachStatus());
  const resolved = resolveBrowserTarget({
    search: window.location.search,
    hash: window.location.hash,
    stored: parseStoredAttach(raw),
  });
  if (!resolved.ok) {
    remoteTarget = null;
    remoteAttach.needed = true;
    remoteAttach.error = resolved.error;
    // A record with an address but no key is a refused key from last time.
    const kept = resolved.error === null ? parseStoredAddress(raw) : null;
    if (kept !== null) {
      remoteAttach.lastUrl = daemonWsUrl(kept.host, kept.port);
      remoteAttach.desktopName = kept.desktopName;
      remoteAttach.error = refusedKeyMessage(kept.desktopName, store.durable);
    }
    // Not a host failure — the user has not attached yet.
    daemon.state = "starting";
    return false;
  }
  remoteTarget = resolved.target;
  remoteAttach.needed = false;
  remoteAttach.error = null;
  remoteAttach.desktopName = resolved.target.desktopName ?? null;
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
        if (!isTauri() && isHandshakeRefusal(event, ws.state)) {
          handleRefusedToken();
          return;
        }
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
  remoteAttach.desktopName = null;
  remoteAttach.fallback = null;
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
  } else {
    watchPairing();
    if (remoteTarget === null) {
      // TD-5014: a link the app was opened with wins, even over a saved
      // target (that is how a phone re-pairs); success comes back here.
      const pending = takePendingPairLink();
      if (pending !== null) {
        daemon.state = "starting";
        void pairFromText(pending);
        return;
      }
      if (!applyBrowserTarget()) return;
    }
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
  stopPairingWatch?.();
  stopPairingWatch = null;
  remoteTarget = null;
  remoteAttach.needed = false;
  remoteAttach.error = null;
  remoteAttach.pairing = false;
  remoteAttach.fallback = null;
  ws.state = "stopped";
}