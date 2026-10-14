// Phone pairing flows (TD-5014): the in-app scanner, a typed code, and a
// tstdesk://pair link the shell hands over. Split from connection-status so
// that file stays about the connection; it binds its side at load.
//
// A link from the shell is only ever offered. Any app or web page can open
// one, so it waits on a confirm card and never replaces the saved computer
// by itself. The scanner and the typed form are the person acting, so they
// redeem directly. Neither the code nor a key is ever logged here.

import { remoteAttach } from "./remote-attach.svelte.js";
import {
  bridgeDeviceName,
  canScanPairing,
  COULD_NOT_SCAN,
  installPairingHandlers,
  pairLinkFromTyped,
  parsePairLink,
  redeemPairing,
  replacedKey,
  scanFailureMessage,
  startPairingScan,
  takePendingPairLink,
  type PairLinkParse,
  type ScanResult,
} from "./pairing";
import { daemonWsUrl, type AttachTarget } from "./remote-connect";

/** What the connection store does for pairing. */
export interface PairingHost {
  /** The computer this device has saved, in use or not, or null. */
  saved(): AttachTarget | null;
  /** A pairing worked: save it and connect. */
  usePaired(target: AttachTarget): void;
  /** Back to the saved computer. */
  useSaved(): void;
}

let bound: PairingHost | null = null;
let stopWatch: (() => void) | null = null;

export function bindPairingHost(host: PairingHost): void {
  bound = host;
}

export function savedName(target: AttachTarget | null): string | null {
  return target === null ? null : (target.desktopName ?? target.host);
}

/** "Scan the code on your computer". The result comes back through the shell. */
export function scanToPair(): void {
  remoteAttach.error = null;
  if (!startPairingScan()) remoteAttach.error = COULD_NOT_SCAN;
}

/** A code from the in-app scanner: the person pointed the camera, so it redeems. */
export function pairFromText(text: string): Promise<boolean> {
  return pairWith(parsePairLink(text));
}

/** "Type the code instead": address + code. */
export function pairFromTyped(address: string, code: string): Promise<boolean> {
  return pairWith(pairLinkFromTyped(address, code));
}

/** A link from the shell: show the confirm card and redeem nothing. */
export function offerPairLink(text: string): void {
  const parsed = parsePairLink(text);
  if (!parsed.ok) {
    showPairFailure(parsed.error);
    return;
  }
  remoteAttach.offer = parsed.link;
  remoteAttach.offerSaved = savedName(bound?.saved() ?? null);
  remoteAttach.needed = true;
  remoteAttach.error = null;
  // The card has its own way back.
  remoteAttach.fallback = null;
}

/** A link the app was opened with, taken once and offered. */
export function offerPendingPairLink(): void {
  const pending = takePendingPairLink();
  if (pending !== null) offerPairLink(pending);
}

/** "Pair" on the confirm card. */
export function acceptPairOffer(): Promise<boolean> {
  const link = remoteAttach.offer;
  if (link === null) return Promise.resolve(false);
  return pairWith({ ok: true, link });
}

/** "Back to <saved>" or "Not now" on the confirm card. */
export function declinePairOffer(): void {
  const hadSaved = remoteAttach.offerSaved !== null;
  clearPairOffer();
  if (hadSaved) bound?.useSaved();
}

export function clearPairOffer(): void {
  remoteAttach.offer = null;
  remoteAttach.offerSaved = null;
}

async function pairWith(parsed: PairLinkParse): Promise<boolean> {
  if (remoteAttach.pairing) return false;
  clearPairOffer();
  if (!parsed.ok) {
    showPairFailure(parsed.error);
    return false;
  }
  const link = parsed.link;
  remoteAttach.needed = true;
  // The person chose to pair; the form shows how it goes, not "Opening…".
  remoteAttach.opening = false;
  remoteAttach.pairing = true;
  remoteAttach.error = null;
  remoteAttach.lastUrl = daemonWsUrl(link.host, link.port);
  const result = await redeemPairing({
    host: link.host,
    port: link.port,
    code: link.code,
    desktopName: link.desktopName,
    deviceName: bridgeDeviceName(),
    replaces: replacedKey(bound?.saved() ?? null, link),
  });
  remoteAttach.pairing = false;
  if (!result.ok) {
    // The store is untouched: a wrong code or a dropped network is not a
    // reason to forget a computer that still works.
    showPairFailure(result.message);
    return false;
  }
  bound?.usePaired(result.target);
  return true;
}

function showPairFailure(message: string): void {
  remoteAttach.needed = true;
  remoteAttach.opening = false;
  remoteAttach.error = message;
  remoteAttach.fallback = savedName(bound?.saved() ?? null);
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
export function watchPairing(): void {
  remoteAttach.canScan = canScanPairing();
  if (stopWatch !== null) return;
  stopWatch = installPairingHandlers({ onScan: handleScan, onPairLink: offerPairLink });
}

export function unwatchPairing(): void {
  stopWatch?.();
  stopWatch = null;
}
