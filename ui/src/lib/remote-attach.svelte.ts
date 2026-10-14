// What a phone or browser tab shows about its saved computer (TD-3701,
// TD-5005, TD-5014). Its own module so the connection store and the pairing
// flows (phone-pairing.ts) share it without importing each other.

import type { PairLink } from "./pairing";

export interface RemoteAttachState {
  /** Browser-only: the connect form is up. */
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
  /** Why the shell could not open or save the connection, in plain words. */
  storeNote: string | null;
  /** A saved computer that still works, to go back to after a failed re-pair. */
  fallback: string | null;
  /** A tstdesk://pair link from the shell, waiting on the confirm card. It is
   *  never redeemed until the person says so. */
  offer: PairLink | null;
  /** The saved computer the offer would replace, by name, or null. */
  offerSaved: string | null;
  /** The key store did not answer at launch; looking again before the form. */
  opening: boolean;
  /** The form offers "Try again" for a saved connection that may still open. */
  canRetryStore: boolean;
  /** The saved computer has not answered for a while (connection-banner.ts). */
  unreachable: boolean;
  /** A handshake message that is not a refusal, for the banner. */
  note: string | null;
}

export const remoteAttach = $state<RemoteAttachState>({
  needed: false,
  error: null,
  lastUrl: "",
  durable: false,
  desktopName: null,
  pairing: false,
  canScan: false,
  storeNote: null,
  fallback: null,
  offer: null,
  offerSaved: null,
  opening: false,
  canRetryStore: false,
  unreachable: false,
  note: null,
});
