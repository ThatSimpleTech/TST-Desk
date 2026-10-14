// The desktop's "Pair a phone" dialog state (TD-5014). The daemon seam is
// mocked exactly as settings.test.ts does it: sendToDaemon out, events in.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, PairingCode } from "./protocol";

const mocks = vi.hoisted(() => ({ sent: [] as ClientMessageUnion[], sendOk: true }));

vi.mock("./connection-status.svelte.js", () => ({
  sendToDaemon: (msg: ClientMessageUnion) => {
    mocks.sent.push(msg);
    return mocks.sendOk;
  },
}));

import {
  cancelPairDialog,
  closePairDialog,
  openPairDialog,
  pairDialog,
  PAIRED_CLOSE_MS,
  reducePairing,
  removeRemoteDevice,
} from "./pair-device.svelte.js";

const CODE: PairingCode = {
  type: "pairing_code",
  seq: 1,
  code: "ABCD2345",
  display_code: "ABCD-2345",
  host: "100.64.7.7",
  port: 39079,
  desktop_name: "ezer",
  link: "tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer",
  qr_svg: "<svg></svg>",
  expires_at: "2026-10-14T10:50:00Z",
};

const DEVICE = {
  id: "dev_0123456789ab",
  name: "Pixel 11 Pro Fold",
  paired_at: "2026-10-14T10:40:00Z",
  last_seen: null,
  connected: true,
};

beforeEach(() => {
  mocks.sent.length = 0;
  mocks.sendOk = true;
  closePairDialog();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("pair dialog (TD-5014)", () => {
  it("asks for a code and shows the one that comes back", () => {
    openPairDialog();
    expect(mocks.sent).toEqual([{ type: "start_pairing" }]);
    expect(pairDialog.waiting).toBe(true);
    reducePairing(CODE);
    expect(pairDialog.offer?.display_code).toBe("ABCD-2345");
    expect(pairDialog.waiting).toBe(false);
  });

  it("ignores a code it did not ask for", () => {
    reducePairing(CODE);
    expect(pairDialog.offer).toBeNull();
  });

  it("cancel tells the daemon to drop the code", () => {
    openPairDialog();
    reducePairing(CODE);
    cancelPairDialog();
    expect(mocks.sent.at(-1)).toEqual({ type: "cancel_pairing" });
    expect(pairDialog.open).toBe(false);
  });

  it("shows who paired, refreshes setup state, and closes itself", () => {
    vi.useFakeTimers();
    openPairDialog();
    reducePairing(CODE);
    reducePairing({ type: "pairing_completed", seq: 1, device: DEVICE });
    expect(pairDialog.paired?.name).toBe("Pixel 11 Pro Fold");
    expect(mocks.sent.at(-1)).toEqual({ type: "get_setup_state" });
    vi.advanceTimersByTime(PAIRED_CLOSE_MS);
    expect(pairDialog.open).toBe(false);
  });

  it("refreshes the list even with no dialog open", () => {
    reducePairing({ type: "pairing_completed", seq: 1, device: DEVICE });
    expect(mocks.sent).toEqual([{ type: "get_setup_state" }]);
    expect(pairDialog.paired).toBeNull();
  });

  it("shows the daemon's refusal in the dialog", () => {
    openPairDialog();
    reducePairing({
      type: "error",
      seq: 1,
      code: "pairing_unavailable",
      message: "Tailscale isn't running on this computer, so a phone can't reach it.",
    } as DaemonEventUnion);
    expect(pairDialog.error).toMatch(/Tailscale isn't running/);
    expect(pairDialog.waiting).toBe(false);
  });

  it("leaves a session's own error alone", () => {
    openPairDialog();
    reducePairing(CODE);
    reducePairing({ type: "error", seq: 3, session_id: "s1", code: "provider", message: "x" } as DaemonEventUnion);
    expect(pairDialog.error).toBeNull();
  });

  it("says so when the daemon is not connected", () => {
    mocks.sendOk = false;
    openPairDialog();
    expect(pairDialog.error).not.toBeNull();
    expect(pairDialog.waiting).toBe(false);
  });

  it("removes a device by id", () => {
    removeRemoteDevice("dev_0123456789ab");
    expect(mocks.sent).toEqual([{ type: "remove_remote_device", device_id: "dev_0123456789ab" }]);
  });
});
