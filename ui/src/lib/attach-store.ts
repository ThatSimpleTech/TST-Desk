// Where a remote client keeps its attach target (TD-3701, TD-5005).
//
// Inside the Android shell the target (address + token) is kept by the
// native side, encrypted with an Android Keystore key, so the phone
// reconnects on launch. A plain browser tab keeps the TD-3701 rule: the
// token lives in sessionStorage for that tab only.

export const REMOTE_ATTACH_STORAGE = "tstdesk.remoteAttach";

/** The `TstDeskNative` object the Android shell injects into the WebView. */
export interface NativeAttachBridge {
  loadAttach(): string | null;
  saveAttach(raw: string): boolean;
  clearAttach(): void;
  // TD-5014 pairing. Optional: a 0.1.5 shell has only the three above and
  // must keep working, so callers check each one before use (see pairing.ts).
  deviceName?(): string;
  scanPairing?(): boolean;
  takePairLink?(): string | null;
  attachStatus?(): string;
}

/** Result of the shell's last loadAttach() (TD-5014). */
export type AttachStatus = "none" | "ok" | "unreadable" | "unavailable";

export interface AttachStore {
  /** True when the target survives closing the app. */
  readonly durable: boolean;
  load(): string | null;
  save(raw: string): void;
  clear(): void;
}

/** The Android bridge, or null outside the shell (or an older shell). */
export function nativeAttachBridge(scope: object = globalThis): NativeAttachBridge | null {
  const candidate = (scope as { TstDeskNative?: Partial<NativeAttachBridge> }).TstDeskNative;
  if (
    candidate === undefined ||
    typeof candidate.loadAttach !== "function" ||
    typeof candidate.saveAttach !== "function" ||
    typeof candidate.clearAttach !== "function"
  ) {
    return null;
  }
  return candidate as NativeAttachBridge;
}

function nativeStore(bridge: NativeAttachBridge): AttachStore {
  return {
    durable: true,
    load: () => {
      try {
        return bridge.loadAttach() || null;
      } catch {
        return null;
      }
    },
    save: (raw) => {
      try {
        bridge.saveAttach(raw);
      } catch {
        // Keystore unavailable: this launch still has the target in memory.
      }
    },
    clear: () => {
      try {
        bridge.clearAttach();
      } catch {
        // Nothing saved, or nothing to do.
      }
    },
  };
}

function sessionStore(storage: Storage): AttachStore {
  return {
    durable: false,
    load: () => {
      try {
        return storage.getItem(REMOTE_ATTACH_STORAGE);
      } catch {
        return null;
      }
    },
    save: (raw) => {
      try {
        storage.setItem(REMOTE_ATTACH_STORAGE, raw);
      } catch {
        // Quota or private mode: reconnect in this tab still works from memory.
      }
    },
    clear: () => {
      try {
        storage.removeItem(REMOTE_ATTACH_STORAGE);
      } catch {
        // Nothing to clear.
      }
    },
  };
}

const NO_STORE: AttachStore = { durable: false, load: () => null, save: () => {}, clear: () => {} };

/** Native store in the Android shell, sessionStorage in a browser tab. */
export function attachStore(scope: object = globalThis): AttachStore {
  const bridge = nativeAttachBridge(scope);
  if (bridge !== null) return nativeStore(bridge);
  const storage = (scope as { sessionStorage?: Storage }).sessionStorage;
  return storage === undefined ? NO_STORE : sessionStore(storage);
}
