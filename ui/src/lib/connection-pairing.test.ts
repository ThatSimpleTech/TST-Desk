// What the phone keeps and forgets around pairing (TD-5014).
//
// The connection store owns the saved target, so these drive it the way the
// app does: a fake ProtocolClient stands in for the socket, a fake shell
// bridge for the Android Keystore store, and redeemPairing is stubbed so no
// socket opens. Each test loads a fresh module, as a relaunch would.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DaemonEventUnion } from "./protocol";
import type { RedeemResult } from "./pairing";

const mocks = vi.hoisted(() => ({
  clients: [] as Array<{
    sink: { onEvent?: (e: unknown) => void; onStateChange?: (s: string) => void };
    started: number;
    stopped: number;
  }>,
  redeem: null as null | ((opts: unknown) => Promise<RedeemResult>),
  redeemCalls: [] as unknown[],
}));

vi.mock("./client", () => ({
  ProtocolClient: class {
    sink: (typeof mocks.clients)[number]["sink"];
    started = 0;
    stopped = 0;
    constructor(_opts: unknown, sink: (typeof mocks.clients)[number]["sink"]) {
      this.sink = sink;
      mocks.clients.push(this);
    }
    async start(): Promise<void> {
      this.started += 1;
    }
    stop(): void {
      this.stopped += 1;
      this.sink.onStateChange?.("stopped");
    }
    send(): boolean {
      return false;
    }
  },
}));
vi.mock("./session-status.svelte.js", () => ({ bindClient: () => {}, ingestEvent: () => {}, resetSession: () => {} }));
vi.mock("./notifications.svelte.js", () => ({ clearNotifications: () => {}, notifyEvent: () => {} }));
vi.mock("./resume", () => ({ watchResume: () => () => {} }));
vi.mock("./open-file", () => ({ isTauri: () => false }));
vi.mock("./pairing", async (original) => ({
  ...(await original<typeof import("./pairing")>()),
  redeemPairing: (opts: unknown) => {
    mocks.redeemCalls.push(opts);
    return mocks.redeem!(opts);
  },
}));

const KEY = "b".repeat(64);
const NEW_KEY = "c".repeat(64);
const LINK = "tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer";
const SAVED = JSON.stringify({
  host: "100.64.7.7",
  port: 39079,
  token: KEY,
  deviceId: "dev_0123456789ab",
  desktopName: "ezer",
});

function shell(saved: string | null, extra: Record<string, unknown> = {}) {
  const state = { saved, link: null as string | null, status: "ok" };
  return {
    state,
    bridge: {
      loadAttach: () => state.saved,
      saveAttach: (raw: string) => {
        state.saved = raw;
        return true;
      },
      clearAttach: () => {
        state.saved = null;
      },
      deviceName: () => "Pixel 11 Pro Fold",
      scanPairing: () => true,
      takePairLink: () => {
        const link = state.link;
        state.link = null;
        return link;
      },
      attachStatus: () => state.status,
      ...extra,
    },
  };
}

async function load() {
  vi.resetModules();
  return import("./connection-status.svelte.js");
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  mocks.clients.length = 0;
  mocks.redeemCalls.length = 0;
  mocks.redeem = null;
  vi.stubGlobal("window", {
    location: { search: "", hash: "", href: "http://localhost/" },
    history: { replaceState: () => {} },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a saved key the daemon refuses (TD-5014)", () => {
  it("keeps the address and the computer's name, and clears only the token", async () => {
    const phone = shell(SAVED);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    expect(conn.remoteAttach.desktopName).toBe("ezer");

    const client = mocks.clients[0];
    client.sink.onStateChange?.("connecting");
    client.sink.onEvent?.({ type: "error", code: "auth_failed", message: "Invalid auth token." } as DaemonEventUnion);

    expect(JSON.parse(phone.state.saved!)).toEqual({
      host: "100.64.7.7",
      port: 39079,
      token: "",
      desktopName: "ezer",
    });
    expect(conn.remoteAttach.needed).toBe(true);
    expect(conn.remoteAttach.lastUrl).toBe("ws://100.64.7.7:39079");
    expect(conn.remoteAttach.error).toBe(
      "This phone isn't paired with ezer anymore. Scan the code on the computer again.",
    );
  });

  it("asks to pair again on the next launch, with the address still filled in", async () => {
    const phone = shell(JSON.stringify({ host: "100.64.7.7", port: 39079, token: "", desktopName: "ezer" }));
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    expect(mocks.clients).toHaveLength(0);
    expect(conn.remoteAttach.needed).toBe(true);
    expect(conn.remoteAttach.lastUrl).toBe("ws://100.64.7.7:39079");
    expect(conn.remoteAttach.error).toContain("isn't paired with ezer");
    expect(phone.state.saved).not.toBeNull();
  });
});

describe("a network failure (TD-5014)", () => {
  it("never touches the saved target", async () => {
    const phone = shell(SAVED);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    const client = mocks.clients[0];
    for (const state of ["connecting", "reconnecting", "disconnected", "reconnecting"]) {
      client.sink.onStateChange?.(state);
    }
    expect(phone.state.saved).toBe(SAVED);
    expect(conn.remoteAttach.needed).toBe(false);
    expect(client.stopped).toBe(0);
  });
});

describe("pairing at launch (TD-5014)", () => {
  it("redeems a pending link even over a saved target, then connects with the new key", async () => {
    const phone = shell(SAVED);
    phone.state.link = LINK;
    vi.stubGlobal("TstDeskNative", phone.bridge);
    mocks.redeem = async () => ({
      ok: true,
      target: { host: "100.64.7.7", port: 39079, token: NEW_KEY, deviceId: "dev_ba9876543210", desktopName: "ezer" },
    });
    const conn = await load();
    await conn.connect();
    await flush();

    expect(mocks.redeemCalls[0]).toMatchObject({ code: "ABCD2345", deviceName: "Pixel 11 Pro Fold", port: 39079 });
    expect(JSON.parse(phone.state.saved!)).toMatchObject({ token: NEW_KEY, deviceId: "dev_ba9876543210", desktopName: "ezer" });
    expect(conn.remoteAttach.needed).toBe(false);
    expect(mocks.clients[0].started).toBe(1);
  });

  it("keeps the saved target when the code fails, and offers to go back to it", async () => {
    const phone = shell(SAVED);
    phone.state.link = LINK;
    vi.stubGlobal("TstDeskNative", phone.bridge);
    mocks.redeem = async () => ({
      ok: false,
      code: "pair_failed",
      message: "That pairing code didn't work. Start pairing again on the computer.",
    });
    const conn = await load();
    await conn.connect();
    await flush();

    expect(phone.state.saved).toBe(SAVED);
    expect(conn.remoteAttach.error).toMatch(/didn't work/);
    expect(conn.remoteAttach.fallback).toBe("ezer");

    conn.useSavedTarget();
    await flush();
    expect(conn.remoteAttach.needed).toBe(false);
    expect(mocks.clients[0].started).toBe(1);
  });

  it("pairs from a link the shell hands over while the page is open", async () => {
    const phone = shell(null);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    mocks.redeem = async () => ({
      ok: true,
      target: { host: "100.64.7.7", port: 39079, token: NEW_KEY, deviceId: "dev_ba9876543210", desktopName: "ezer" },
    });
    const conn = await load();
    await conn.connect();
    expect(conn.remoteAttach.needed).toBe(true);
    expect(conn.remoteAttach.canScan).toBe(true);

    (globalThis as unknown as { __tstDeskOnPairLink: (raw: unknown) => void }).__tstDeskOnPairLink(LINK);
    await flush();
    expect(JSON.parse(phone.state.saved!)).toMatchObject({ token: NEW_KEY });
    expect(conn.remoteAttach.desktopName).toBe("ezer");
    conn.disconnect();
  });
});

describe("a saved connection the shell could not open (TD-5014)", () => {
  it("says so on the connect form", async () => {
    const phone = shell(null);
    phone.state.status = "unavailable";
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    expect(conn.remoteAttach.needed).toBe(true);
    expect(conn.remoteAttach.storeNote).toMatch(/key store/);
  });
});

describe("a browser tab (TD-3701)", () => {
  it("keeps a paired key in sessionStorage only", async () => {
    const session = new Map<string, string>();
    vi.stubGlobal("sessionStorage", {
      getItem: (k: string) => session.get(k) ?? null,
      setItem: (k: string, v: string) => void session.set(k, v),
      removeItem: (k: string) => void session.delete(k),
    });
    mocks.redeem = async () => ({
      ok: true,
      target: { host: "100.64.7.7", port: 39079, token: NEW_KEY, deviceId: "dev_ba9876543210", desktopName: "ezer" },
    });
    const conn = await load();
    await conn.connect();
    expect(conn.remoteAttach.canScan).toBe(false);
    expect(await conn.pairFromTyped("100.64.7.7:39079", "abcd-2345")).toBe(true);
    expect(mocks.redeemCalls[0]).toMatchObject({ deviceName: "Web browser" });
    expect(conn.remoteAttach.durable).toBe(false);
    expect([...session.values()][0]).toContain(NEW_KEY);
  });
});
