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
import { watchResume } from "./resume";
import { isTauri } from "./open-file";
import { attachStore } from "./attach-store";
import { clearNotifications, notify, notifyEvent } from "./notifications.svelte.js";
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
import { attachStatusNote, DEVICES_UNAVAILABLE, readAttachStatus, refusedKeyMessage } from "./pairing";
import { remoteAttach } from "./remote-attach.svelte.js";
import {
  bindPairingHost,
  clearPairOffer,
  offerPendingPairLink,
  savedName,
  unwatchPairing,
  watchPairing,
} from "./phone-pairing";
import { StoreRetry, UnreachableWatch } from "./remote-watch";
import { UNREACHABLE_AFTER_MS } from "./connection-banner";
import { noteHandshake } from "./hub.svelte.js";
import { refusedForBridged } from "./bridged-id";

export { remoteAttach } from "./remote-attach.svelte.js";
export {
  acceptPairOffer,
  declinePairOffer,
  pairFromText,
  pairFromTyped,
  scanToPair,
} from "./phone-pairing";

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

let remoteTarget: AttachTarget | null = null;
/** A target the durable store refused to save, retried once after hello_ack. */
let unsavedTarget: AttachTarget | null = null;

const COULD_NOT_SAVE = "This phone couldn't save the connection, so it will ask again next launch.";

// TD-5014: Android's key store not answering at launch is usually brief, so
// look again before settling on the form; and a saved computer that stays
// silent gets "Can't reach" and "Pair again" in the banner.
const storeRetry = new StoreRetry(reopenSavedTarget, () => {
  remoteAttach.opening = false;
  remoteAttach.canRetryStore = true;
});
const unreachable = new UnreachableWatch(UNREACHABLE_AFTER_MS, (on) => {
  remoteAttach.unreachable = on;
});

bindPairingHost({
  saved: () => remoteTarget ?? loadStoredTarget(),
  usePaired: usePairedTarget,
  useSaved: useSavedTarget,
});

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
  // A verb the hub won't forward for another computer's session would only
  // ask this daemon about a session it doesn't own (TD-5015).
  if (refusedForBridged(msg)) return false;
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
  // TD-5014: the key store can refuse a write it takes a moment later. Try
  // once more after the computer has taken the key, then say so.
  unsavedTarget = store.save(serializeAttach(target)) || !store.durable ? null : target;
}

function retryUnsavedTarget(): void {
  const target = unsavedTarget;
  unsavedTarget = null;
  if (target === null || target !== remoteTarget) return;
  if (attachStore().save(serializeAttach(target))) return;
  remoteAttach.storeNote = COULD_NOT_SAVE;
  notify("attach-unsaved", { severity: "banner", title: "Connection not saved", body: COULD_NOT_SAVE });
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
  unsavedTarget = null;
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
  // TD-5014: tell the computer first, so its list drops this phone now
  // instead of keeping it as paired and never seen again.
  const deviceId = remoteTarget?.deviceId;
  if (deviceId !== undefined && ws.state === "connected") {
    sendToDaemon({ type: "remove_remote_device", device_id: deviceId });
  }
  client?.stop();
  storeRetry.stop();
  attachStore().clear();
  remoteAttach.lastUrl = remoteTarget === null ? "" : daemonWsUrl(remoteTarget.host, remoteTarget.port);
  remoteTarget = null;
  unsavedTarget = null;
  clearPairOffer();
  remoteAttach.needed = true;
  remoteAttach.error = null;
  remoteAttach.desktopName = null;
  remoteAttach.fallback = null;
  remoteAttach.opening = false;
  remoteAttach.canRetryStore = false;
}

/** Back to the saved computer after a failed or declined re-pair. */
export function useSavedTarget(): void {
  remoteAttach.error = null;
  remoteAttach.fallback = null;
  if (remoteTarget !== null) {
    remoteAttach.needed = false;
    return;
  }
  void connect();
}

/** The banner's "Pair again" (TD-5014): the form over a computer that has
 *  stopped answering. Nothing is forgotten; the saved computer keeps
 *  retrying until a new pairing replaces it. */
export function pairAgain(): void {
  if (remoteTarget === null) return;
  remoteAttach.needed = true;
  remoteAttach.error = null;
  remoteAttach.fallback = savedName(remoteTarget);
}

/** "Try again" next to the note about a saved connection that would not open. */
export function retrySavedConnection(): void {
  storeRetry.retryNow();
}

function usePairedTarget(target: AttachTarget): void {
  const previous = remoteTarget;
  storeRetry.stop();
  remoteTarget = target;
  persistRemoteTarget(target);
  remoteAttach.needed = false;
  remoteAttach.error = null;
  remoteAttach.fallback = null;
  remoteAttach.storeNote = null;
  remoteAttach.opening = false;
  remoteAttach.canRetryStore = false;
  remoteAttach.note = null;
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

/** True for the daemon's handshake refusal, not a provider's own auth error. */
export function isHandshakeRefusal(event: DaemonEventUnion, state: ConnectionState): boolean {
  return handshakeError(event, state, "auth_failed");
}

/** TD-5014: the computer couldn't read its device list. Not a refusal: the
 *  key stays, the client retries on its backoff, the banner says why. */
export function isHandshakeNotice(
  event: DaemonEventUnion,
  state: ConnectionState,
): event is Extract<DaemonEventUnion, { type: "error" }> {
  return handshakeError(event, state, DEVICES_UNAVAILABLE);
}

function handshakeError(event: DaemonEventUnion, state: ConnectionState, code: string): boolean {
  return event.type === "error" && event.code === code && (event.session_id ?? null) === null && state !== "connected";
}

/** Use a saved or URL target. The form goes away unless a link offer waits. */
function adoptTarget(target: AttachTarget): void {
  remoteTarget = target;
  remoteAttach.durable = attachStore().durable;
  remoteAttach.needed = remoteAttach.offer !== null;
  if (remoteAttach.offer !== null) remoteAttach.offerSaved = savedName(target);
  remoteAttach.error = null;
  remoteAttach.storeNote = null;
  remoteAttach.opening = false;
  remoteAttach.canRetryStore = false;
  remoteAttach.desktopName = target.desktopName ?? null;
  daemon.state = "starting";
  daemon.port = target.port;
}

/** A record with an address but no key is a refused key from last time. */
function showKeptAddress(raw: string | null, durable: boolean): void {
  const kept = parseStoredAddress(raw);
  if (kept === null) return;
  remoteAttach.lastUrl = daemonWsUrl(kept.host, kept.port);
  remoteAttach.desktopName = kept.desktopName;
  remoteAttach.error = refusedKeyMessage(kept.desktopName, durable);
}

/** One more look at a saved connection the key store would not open. True
 *  once there is nothing left to retry. */
function reopenSavedTarget(): boolean {
  if (remoteTarget !== null) return true;
  if (remoteAttach.pairing) return false;
  const store = attachStore();
  const raw = store.load();
  const status = readAttachStatus();
  const target = parseStoredAttach(raw);
  if (target !== null) {
    adoptTarget(target);
    void connect();
    return true;
  }
  if (status === "unavailable") return false;
  remoteAttach.opening = false;
  remoteAttach.canRetryStore = false;
  remoteAttach.storeNote = attachStatusNote(status);
  showKeptAddress(raw, store.durable);
  return true;
}

function applyBrowserTarget(): boolean {
  if (typeof window === "undefined") return false;
  const store = attachStore();
  const raw = store.load();
  const status = readAttachStatus();
  // TD-5014: say plainly when the shell could not open what it saved, so a
  // phone that "forgot" is diagnosable from the form itself.
  remoteAttach.storeNote = attachStatusNote(status);
  const resolved = resolveBrowserTarget({
    search: window.location.search,
    hash: window.location.hash,
    stored: parseStoredAttach(raw),
  });
  if (!resolved.ok) {
    remoteTarget = null;
    remoteAttach.needed = true;
    remoteAttach.error = resolved.error;
    if (resolved.error === null) {
      showKeptAddress(raw, store.durable);
      if (status === "unavailable") {
        remoteAttach.opening = true;
        storeRetry.start(typeof document === "undefined" ? null : document);
      }
    }
    // Not a host failure — the user has not attached yet.
    daemon.state = "starting";
    return false;
  }
  adoptTarget(resolved.target);
  // A stored target is already saved; writing it back would only invent a
  // failure to report.
  if (resolved.from === "url") persistRemoteTarget(resolved.target);
  if (resolved.stripUrl) {
    window.history.replaceState(null, "", stripAttachFromUrl(window.location.href));
  }
  return true;
}

/** Phone-side bookkeeping on every connection state (TD-5014). */
function watchRemoteState(state: ConnectionState): void {
  if (state === "connected" || state === "stopped") remoteAttach.note = null;
  if (state === "connected") retryUnsavedTarget();
  unreachable.update(state, remoteTarget !== null);
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
        if (!isTauri() && isHandshakeNotice(event, ws.state)) {
          remoteAttach.note = event.message;
          return;
        }
        lastEvent.event = event;
        for (const sub of eventSubs) sub(event);
        ingestEvent(event); // wire the session reducer (TD-1006)
        notifyEvent(event); // turn failures / pauses / errors into notices (TD-1008)
      },
      onHandshake(ack) {
        noteHandshake(ack); // before "connected", so stores see who answered (TD-5015)
      },
      onStateChange(state) {
        ws.state = state;
        syncRemoteDaemonMirror(state);
        if (!isTauri()) watchRemoteState(state);
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
  clearPairOffer();
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
      const ready = applyBrowserTarget();
      // TD-5014: a link the app was opened with is only offered; the saved
      // computer, if any, keeps connecting while the card waits.
      offerPendingPairLink();
      if (!ready) return;
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
  unwatchPairing();
  storeRetry.stop();
  unreachable.reset();
  remoteTarget = null;
  unsavedTarget = null;
  clearPairOffer();
  remoteAttach.needed = false;
  remoteAttach.error = null;
  remoteAttach.pairing = false;
  remoteAttach.fallback = null;
  remoteAttach.opening = false;
  remoteAttach.note = null;
  ws.state = "stopped";
}