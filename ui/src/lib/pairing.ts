// Phone pairing (TD-5014).
//
// The computer shows a code (and a QR of a tstdesk://pair link). The phone
// redeems it once over the daemon's remote listener and gets its own key,
// which the attach store keeps; after that it connects with a normal hello.
// A scanned QR, a link the app was opened with, and a typed address + code
// all go through parsePairLink, so there is one set of rules for all three.
//
// Neither the code nor the key is ever logged here.

import type { SocketFactory, SocketLike } from "./client";
import type { Pair } from "./protocol";
import { nativeAttachBridge, type AttachStatus, type NativeAttachBridge } from "./attach-store";
import {
  cleanName,
  daemonWsUrl,
  isRemoteShellHost,
  parseWsUrl,
  unwrapHost,
  type AttachTarget,
} from "./remote-connect";

/** Crockford base32, as the daemon mints it. */
export const PAIR_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const PAIR_CODE_LENGTH = 8;
export const PAIR_TIMEOUT_MS = 10_000;
const PAIR_PROTOCOL_VERSION = 1;
const UNNAMED_DEVICE = "Unnamed device";
const DEVICE_KEY = /^[0-9a-f]{64}$/;
const DEVICE_ID = /^dev_[0-9a-f]{12}$/;

/** Uppercase, drop spaces and dashes, read O as 0 and I/L as 1. Null unless
 *  exactly 8 characters of the alphabet remain. */
export function normalizePairCode(input: string): string | null {
  const code = input
    .toUpperCase()
    .replace(/[\s-]/g, "")
    .replace(/O/g, "0")
    .replace(/[IL]/g, "1");
  if (code.length !== PAIR_CODE_LENGTH) return null;
  for (const ch of code) {
    if (!PAIR_CODE_ALPHABET.includes(ch)) return null;
  }
  return code;
}

/** "ABCD2345" -> "ABCD-2345". Anything else comes back as given. */
export function formatPairCode(code: string): string {
  const normal = normalizePairCode(code);
  return normal === null ? code : `${normal.slice(0, 4)}-${normal.slice(4)}`;
}

export interface PairLink {
  host: string;
  port: number;
  code: string;
  /** The computer's name from the link, when it carried one. */
  desktopName: string | null;
}

export type PairLinkParse = { ok: true; link: PairLink } | { ok: false; error: string };

const NOT_A_PAIR_CODE = "That isn't a TST Desk pairing code. Scan the code in Settings on the computer.";
const NOT_TAILSCALE = "That address isn't on Tailscale, so this app won't connect to it.";
const BAD_PORT = "The address in that code has no usable port.";
const BAD_CODE = "The pairing code is 8 letters and numbers, like ABCD-2345.";

/** Parse `tstdesk://pair?h=<host>&p=<port>&c=<code>&n=<name>`. The host must
 *  pass the phone shell's Tailscale rule (remote-connect.ts), always: the
 *  daemon only pairs on its Tailscale listener. */
export function parsePairLink(text: string): PairLinkParse {
  const trimmed = text.trim();
  const query = trimmed.indexOf("?");
  if (query < 0) return { ok: false, error: NOT_A_PAIR_CODE };
  const head = trimmed.slice(0, query).toLowerCase();
  if (head !== "tstdesk://pair" && head !== "tstdesk://pair/") {
    return { ok: false, error: NOT_A_PAIR_CODE };
  }
  const fragment = trimmed.indexOf("#", query);
  const params = new URLSearchParams(trimmed.slice(query + 1, fragment < 0 ? undefined : fragment));
  const rawHost = params.get("h");
  const rawPort = params.get("p");
  const rawCode = params.get("c");
  if (rawHost === null || rawPort === null || rawCode === null) {
    return { ok: false, error: NOT_A_PAIR_CODE };
  }
  const host = unwrapHost(rawHost.trim());
  if (!isRemoteShellHost(host)) return { ok: false, error: NOT_TAILSCALE };
  const port = /^[0-9]{1,5}$/.test(rawPort.trim()) ? Number(rawPort.trim()) : NaN;
  if (!Number.isInteger(port) || port < 1 || port > 65535) return { ok: false, error: BAD_PORT };
  const code = normalizePairCode(rawCode);
  if (code === null) return { ok: false, error: BAD_CODE };
  const name = cleanName(params.get("n") ?? "");
  return { ok: true, link: { host, port, code, desktopName: name === "" ? null : name } };
}

/** Typed address (`host:port`) + code, turned into a link and parsed like a scan. */
export function pairLinkFromTyped(address: string, code: string): PairLinkParse {
  if (address.trim() === "") {
    return { ok: false, error: "Enter the address shown on the computer, like 100.64.0.1:39079." };
  }
  // Split host:port only; the Tailscale rule is parsePairLink's, worded once.
  const parsed = parseWsUrl(address, "any-specific");
  if (!parsed.ok) return { ok: false, error: parsed.error };
  const normal = normalizePairCode(code);
  if (normal === null) return { ok: false, error: BAD_CODE };
  const params = new URLSearchParams({ h: parsed.host, p: String(parsed.port), c: normal });
  return parsePairLink(`tstdesk://pair?${params.toString()}`);
}

export type RedeemResult =
  | { ok: true; target: AttachTarget }
  | { ok: false; message: string; code?: string };

export interface RedeemOptions {
  host: string;
  port: number;
  code: string;
  deviceName: string;
  /** For the copy only; the daemon's own name wins once it answers. */
  desktopName?: string | null;
  /** This phone's current device key, so the daemon drops the entry it
   *  replaces. Sent only when it is one; never a legacy remote-token. */
  replaces?: string | null;
  socketFactory?: SocketFactory;
  timeoutMs?: number;
  timers?: {
    set(fn: () => void, ms: number): unknown;
    clear(handle: unknown): void;
  };
}

const browserSocket: SocketFactory = (url) => new WebSocket(url) as SocketLike;
const realTimers = {
  set: (fn: () => void, ms: number): unknown => setTimeout(fn, ms),
  clear: (handle: unknown): void => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** Open ws://host:port, send the pair frame, wait for pair_ok or an error
 *  frame, close. Never rejects; every failure is a message to show. */
export function redeemPairing(opts: RedeemOptions): Promise<RedeemResult> {
  const where = opts.desktopName ?? "the computer";
  const unreachable = `Couldn't reach ${where}. Check that Tailscale is on for both, then try again.`;
  const timers = opts.timers ?? realTimers;
  return new Promise((resolve) => {
    let socket: SocketLike | null = null;
    let timer: unknown = null;
    let settled = false;
    const finish = (result: RedeemResult): void => {
      if (settled) return;
      settled = true;
      if (timer !== null) timers.clear(timer);
      const open = socket;
      socket = null;
      if (open !== null) {
        open.onopen = null;
        open.onmessage = null;
        open.onclose = null;
        open.onerror = null;
        try {
          open.close(1000);
        } catch {
          // Already closed.
        }
      }
      resolve(result);
    };
    try {
      socket = (opts.socketFactory ?? browserSocket)(daemonWsUrl(opts.host, opts.port));
    } catch {
      finish({ ok: false, message: unreachable });
      return;
    }
    const noAnswer = `No answer from ${where} in time. Check that Tailscale is on for both, then try again.`;
    timer = timers.set(() => finish({ ok: false, message: noAnswer }), opts.timeoutMs ?? PAIR_TIMEOUT_MS);
    const live = socket;
    live.onopen = () => {
      live.send(JSON.stringify(pairFrame(opts)));
    };
    live.onmessage = (ev) => {
      const frame = parseFrame(ev.data);
      if (frame === null) return;
      if (frame.type === "pair_ok") finish(pairOkResult(frame, opts));
      else if (frame.type === "error") finish(errorResult(frame));
    };
    // A close or error before an answer is a network failure, never a verdict
    // on the code, so it says nothing about what is saved.
    live.onclose = () => finish({ ok: false, message: unreachable });
    live.onerror = () => finish({ ok: false, message: unreachable });
  });
}

function pairFrame(opts: RedeemOptions): Pair {
  const frame: Pair = {
    type: "pair",
    code: opts.code,
    device_name: cleanDeviceName(opts.deviceName),
    version: PAIR_PROTOCOL_VERSION,
  };
  if (typeof opts.replaces === "string" && DEVICE_KEY.test(opts.replaces)) frame.replaces = opts.replaces;
  return frame;
}

/** The key to send as `replaces` when re-pairing: the saved device key, and
 *  only for the computer that issued it, so a key never goes to another one.
 *  A record without a deviceId holds a legacy remote-token, which never goes. */
export function replacedKey(saved: AttachTarget | null, link: PairLink): string | null {
  if (saved === null || saved.deviceId === undefined) return null;
  if (saved.host !== link.host || saved.port !== link.port) return null;
  return DEVICE_KEY.test(saved.token) ? saved.token : null;
}

function parseFrame(data: unknown): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(String(data));
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function pairOkResult(frame: Record<string, unknown>, opts: RedeemOptions): RedeemResult {
  const { token, device_id: deviceId, desktop_name: rawName } = frame;
  if (typeof token !== "string" || !DEVICE_KEY.test(token) || typeof deviceId !== "string" || !DEVICE_ID.test(deviceId)) {
    return { ok: false, message: "The computer's answer didn't make sense. Update TST Desk on both, then try again." };
  }
  const named = typeof rawName === "string" ? cleanName(rawName) : "";
  const desktopName = named !== "" ? named : (opts.desktopName ?? null);
  const target: AttachTarget = { host: opts.host, port: opts.port, token, deviceId };
  if (desktopName !== null) target.desktopName = desktopName;
  return { ok: true, target };
}

function errorResult(frame: Record<string, unknown>): RedeemResult {
  const message =
    typeof frame.message === "string" && frame.message.trim() !== ""
      ? frame.message
      : "That pairing code didn't work. Start pairing again on the computer.";
  return typeof frame.code === "string" ? { ok: false, message, code: frame.code } : { ok: false, message };
}

/** A saved key was refused (auth_failed). The phone wording is the
 *  contract's; a browser tab cannot scan, so it is told to type a code. */
export function refusedKeyMessage(desktopName: string | null, durable: boolean): string {
  const computer = desktopName ?? "this computer";
  return durable
    ? `This phone isn't paired with ${computer} anymore. Scan the code on the computer again.`
    : `This browser isn't paired with ${computer} anymore. Enter a new code from the computer.`;
}

/** The daemon could not read its device list (an I/O error, not a verdict on
 *  the key). The phone keeps its key and its normal retry; only auth_failed
 *  means the key is gone. */
export const DEVICES_UNAVAILABLE = "devices_unavailable";

/** The confirm card for a link the shell handed over: a link is never
 *  redeemed until the person says so, because it can come from anywhere. */
export function pairOfferCopy(
  link: PairLink,
  savedName: string | null,
): { question: string; replaces: string | null; decline: string } {
  const who = link.desktopName === null ? "the computer" : link.desktopName;
  return {
    question: `Pair with ${who} at ${link.host}?`,
    replaces: savedName === null ? null : `This replaces ${savedName}.`,
    decline: savedName === null ? "Not now" : `Back to ${savedName}`,
  };
}

/** "host:port" for the typed form, IPv6 bracketed as the form expects. */
export function linkAddress(link: PairLink): string {
  return daemonWsUrl(link.host, link.port).replace(/^ws:\/\//, "");
}

/** The name the computer lists this device under: the daemon's rule, applied
 *  here too so the list never shows something the phone did not intend. */
export function cleanDeviceName(raw: string): string {
  const name = cleanName(raw);
  return name === "" ? UNNAMED_DEVICE : name;
}

// ── Shell bridge (all optional; a 0.1.5 shell has none of these) ────────

type Scope = object;

/** Call one optional bridge method; a missing or throwing one reads as undefined. */
function bridgeCall<T>(scope: Scope, pick: (bridge: NativeAttachBridge) => (() => T) | undefined): T | undefined {
  const bridge = nativeAttachBridge(scope);
  if (bridge === null) return undefined;
  const fn = pick(bridge);
  if (typeof fn !== "function") return undefined;
  try {
    return fn.call(bridge);
  } catch {
    return undefined;
  }
}

/** The shell can open its QR scanner. */
export function canScanPairing(scope: Scope = globalThis): boolean {
  const bridge = nativeAttachBridge(scope);
  return bridge !== null && typeof bridge.scanPairing === "function";
}

/** Open the scanner. False when there is none or it could not start; the
 *  result arrives later through `__tstDeskOnScan`. */
export function startPairingScan(scope: Scope = globalThis): boolean {
  return bridgeCall(scope, (b) => b.scanPairing) === true;
}

/** This device's name for the computer's list. A browser tab has no shell to ask. */
export function bridgeDeviceName(scope: Scope = globalThis): string {
  const name = bridgeCall(scope, (b) => b.deviceName);
  return cleanDeviceName(typeof name === "string" && name.trim() !== "" ? name : "Web browser");
}

/** A tstdesk://pair link the app was opened with, taken once. */
export function takePendingPairLink(scope: Scope = globalThis): string | null {
  const link = bridgeCall(scope, (b) => b.takePairLink);
  return typeof link === "string" && link.trim() !== "" ? link : null;
}

/** What the shell's last loadAttach() found, or null from an older shell. */
export function readAttachStatus(scope: Scope = globalThis): AttachStatus | null {
  const status = bridgeCall(scope, (b) => b.attachStatus);
  return status === "none" || status === "ok" || status === "unreadable" || status === "unavailable"
    ? status
    : null;
}

/** Plain words for a saved connection the shell could not open. */
export function attachStatusNote(status: AttachStatus | null): string | null {
  if (status === "unreadable") {
    return "The saved connection on this phone was damaged, so it was cleared. Scan the code on the computer to pair again.";
  }
  if (status === "unavailable") {
    return "Android's key store didn't answer, so the saved connection couldn't be opened. It's still saved; the app tries again next launch.";
  }
  return null;
}

export type ScanResult =
  | { ok: true; text: string }
  | { ok: false; reason: "canceled" | "permission" | "no_camera" | "error" };

/** The shell passes a JSON object; accept a JSON string too, refuse anything else. */
export function normalizeScanResult(raw: unknown): ScanResult {
  let value = raw;
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return { ok: false, reason: "error" };
    }
  }
  if (typeof value !== "object" || value === null) return { ok: false, reason: "error" };
  const rec = value as Record<string, unknown>;
  if (rec.ok === true && typeof rec.text === "string") return { ok: true, text: rec.text };
  const reason = rec.reason;
  if (reason === "canceled" || reason === "permission" || reason === "no_camera") return { ok: false, reason };
  return { ok: false, reason: "error" };
}

/** What to say when a scan did not give a code. Canceling says nothing. */
export function scanFailureMessage(reason: Exclude<ScanResult, { ok: true }>["reason"]): string | null {
  switch (reason) {
    case "canceled":
      return null;
    case "permission":
      return "The app needs the camera to scan the code. Allow it in Android settings, or type the code instead.";
    case "no_camera":
      return "This device has no camera to scan with. Type the code instead.";
    default:
      return "The scanner stopped. Try again, or type the code instead.";
  }
}

export const COULD_NOT_SCAN = "The scanner couldn't start. Type the code instead.";

export interface PairingHandlers {
  onScan(result: ScanResult): void;
  onPairLink(link: string): void;
}

interface PairingGlobals {
  __tstDeskOnScan?: (raw: unknown) => void;
  __tstDeskOnPairLink?: (raw: unknown) => void;
}

/** Install the callbacks the shell calls into. Returns an uninstall fn. */
export function installPairingHandlers(handlers: PairingHandlers, scope: Scope = globalThis): () => void {
  const target = scope as PairingGlobals;
  const onScan = (raw: unknown): void => handlers.onScan(normalizeScanResult(raw));
  const onPairLink = (raw: unknown): void => {
    if (typeof raw === "string" && raw.trim() !== "") handlers.onPairLink(raw);
  };
  target.__tstDeskOnScan = onScan;
  target.__tstDeskOnPairLink = onPairLink;
  return () => {
    if (target.__tstDeskOnScan === onScan) delete target.__tstDeskOnScan;
    if (target.__tstDeskOnPairLink === onPairLink) delete target.__tstDeskOnPairLink;
  };
}

// The desktop list's wording lives in device-words.ts; re-exported for callers.
export { formatCountdown, pairedWords, relativeWords } from "./device-words";
