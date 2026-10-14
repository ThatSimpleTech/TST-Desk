// What the phone keeps and forgets around pairing (TD-5014).
//
// The connection store owns the saved target, so these drive it the way the
// app does: a fake ProtocolClient stands in for the socket, a fake shell
// bridge for the Android Keystore store, and redeemPairing is stubbed so no
// socket opens. Each test loads a fresh module, as a relaunch would.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
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
  /** Sends and stops on the fake client, in order. */
  wire: [] as unknown[],
  notices: [] as unknown[],
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
      mocks.wire.push("stop");
      this.sink.onStateChange?.("stopped");
    }
    send(msg: unknown): boolean {
      mocks.wire.push(msg);
      return true;
    }
  },
}));
vi.mock("./session-status.svelte.js", () => ({ bindClient: () => {}, ingestEvent: () => {}, resetSession: () => {} }));
vi.mock("./notifications.svelte.js", () => ({
  clearNotifications: () => {},
  notifyEvent: () => {},
  notify: (key: string, spec: unknown) => void mocks.notices.push({ key, spec }),
}));
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
const LEGACY = JSON.stringify({ host: "100.64.7.7", port: 39079, token: "legacy-remote-token" });
const PAIRED: RedeemResult = {
  ok: true,
  target: { host: "100.64.7.7", port: 39079, token: NEW_KEY, deviceId: "dev_ba9876543210", desktopName: "ezer" },
};

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

type Hooks = { __tstDeskOnPairLink: (raw: unknown) => void; __tstDeskOnScan: (raw: unknown) => void };
const hooks = () => globalThis as unknown as Hooks;

// The first import transforms the whole connection graph. On a loaded
// machine that alone can pass the 5 s test timeout; later imports reuse it.
beforeAll(async () => {
  await import("./connection-status.svelte.js");
}, 60_000);

beforeEach(() => {
  mocks.clients.length = 0;
  mocks.redeemCalls.length = 0;
  mocks.wire.length = 0;
  mocks.notices.length = 0;
  mocks.redeem = null;
  vi.stubGlobal("window", {
    location: { search: "", hash: "", href: "http://localhost/" },
    history: { replaceState: () => {} },
  });
});

afterEach(() => {
  vi.useRealTimers();
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

describe("a pairing link from the shell (TD-5014)", () => {
  it("is offered, not redeemed, while the saved computer keeps connecting", async () => {
    const phone = shell(SAVED);
    phone.state.link = LINK;
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    await flush();

    expect(mocks.redeemCalls).toHaveLength(0);
    expect(mocks.clients[0].started).toBe(1);
    expect(phone.state.saved).toBe(SAVED);
    expect(conn.remoteAttach.needed).toBe(true);
    expect(conn.remoteAttach.offer).toMatchObject({ host: "100.64.7.7", port: 39079, code: "ABCD2345" });
    expect(conn.remoteAttach.offerSaved).toBe("ezer");
  });

  it("pairs only on Pair, replacing the saved key on the same computer", async () => {
    const phone = shell(SAVED);
    phone.state.link = LINK;
    vi.stubGlobal("TstDeskNative", phone.bridge);
    mocks.redeem = async () => PAIRED;
    const conn = await load();
    await conn.connect();

    expect(await conn.acceptPairOffer()).toBe(true);
    expect(mocks.redeemCalls[0]).toMatchObject({ code: "ABCD2345", deviceName: "Pixel 11 Pro Fold", replaces: KEY });
    expect(JSON.parse(phone.state.saved!)).toMatchObject({ token: NEW_KEY, deviceId: "dev_ba9876543210" });
    expect(conn.remoteAttach.needed).toBe(false);
    expect(conn.remoteAttach.offer).toBeNull();
  });

  it("goes back to the saved computer and changes nothing", async () => {
    const phone = shell(SAVED);
    phone.state.link = LINK;
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();

    conn.declinePairOffer();
    expect(conn.remoteAttach.needed).toBe(false);
    expect(conn.remoteAttach.offer).toBeNull();
    expect(phone.state.saved).toBe(SAVED);
    expect(mocks.redeemCalls).toHaveLength(0);
    expect(mocks.clients[0].stopped).toBe(0);
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
    expect(await conn.acceptPairOffer()).toBe(false);

    expect(phone.state.saved).toBe(SAVED);
    expect(conn.remoteAttach.error).toMatch(/didn't work/);
    expect(conn.remoteAttach.fallback).toBe("ezer");

    conn.useSavedTarget();
    expect(conn.remoteAttach.needed).toBe(false);
    expect(mocks.clients[0].started).toBe(1);
  });

  it("is offered when it arrives while the page is open, with nothing to replace", async () => {
    const phone = shell(null);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    expect(conn.remoteAttach.canScan).toBe(true);

    hooks().__tstDeskOnPairLink(LINK);
    await flush();
    expect(mocks.redeemCalls).toHaveLength(0);
    expect(conn.remoteAttach.offer).not.toBeNull();
    expect(conn.remoteAttach.offerSaved).toBeNull();

    conn.declinePairOffer();
    expect(conn.remoteAttach.offer).toBeNull();
    expect(conn.remoteAttach.needed).toBe(true);
    expect(phone.state.saved).toBeNull();
    conn.disconnect();
  });

  it("never sends a key to a different computer, or a legacy token at all", async () => {
    for (const saved of [LEGACY, SAVED.replace("100.64.7.7", "100.64.0.9")]) {
      mocks.redeemCalls.length = 0;
      const phone = shell(saved);
      phone.state.link = LINK;
      vi.stubGlobal("TstDeskNative", phone.bridge);
      mocks.redeem = async () => PAIRED;
      const conn = await load();
      await conn.connect();
      await conn.acceptPairOffer();
      expect((mocks.redeemCalls[0] as { replaces: unknown }).replaces).toBeNull();
      conn.disconnect();
    }
  });
});

describe("the in-app scanner (TD-5014)", () => {
  it("redeems directly: the person chose to pair", async () => {
    const phone = shell(null);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    mocks.redeem = async () => PAIRED;
    const conn = await load();
    await conn.connect();

    hooks().__tstDeskOnScan({ ok: true, text: LINK });
    await flush();
    expect(mocks.redeemCalls).toHaveLength(1);
    expect(JSON.parse(phone.state.saved!)).toMatchObject({ token: NEW_KEY });
    expect(conn.remoteAttach.desktopName).toBe("ezer");
    conn.disconnect();
  });
});

describe("forgetting this computer (TD-5014 addendum 2)", () => {
  it("tells the computer to drop this device first, when connected", async () => {
    const phone = shell(SAVED);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    mocks.clients[0].sink.onStateChange?.("connected");

    conn.forgetRemoteTarget();
    expect(mocks.wire).toEqual([{ type: "remove_remote_device", device_id: "dev_0123456789ab" }, "stop"]);
    expect(phone.state.saved).toBeNull();
    expect(conn.remoteAttach.needed).toBe(true);
  });

  it("sends nothing while offline", async () => {
    const phone = shell(SAVED);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    mocks.clients[0].sink.onStateChange?.("reconnecting");
    conn.forgetRemoteTarget();
    expect(mocks.wire).toEqual(["stop"]);
  });

  it("sends nothing for a legacy token, which names no device", async () => {
    const phone = shell(LEGACY);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    mocks.clients[0].sink.onStateChange?.("connected");
    conn.forgetRemoteTarget();
    expect(mocks.wire).toEqual(["stop"]);
  });
});

describe("a save the key store refuses (TD-5014)", () => {
  it("is tried once more after hello_ack", async () => {
    let saves = 0;
    const phone = shell(null, {
      saveAttach: (raw: string) => {
        saves += 1;
        if (saves === 1) return false;
        phone.state.saved = raw;
        return true;
      },
    });
    vi.stubGlobal("TstDeskNative", phone.bridge);
    mocks.redeem = async () => PAIRED;
    const conn = await load();
    await conn.connect();
    await conn.pairFromText(LINK);
    expect(phone.state.saved).toBeNull();

    mocks.clients[0].sink.onStateChange?.("connected");
    expect(JSON.parse(phone.state.saved!)).toMatchObject({ token: NEW_KEY });
    expect(conn.remoteAttach.storeNote).toBeNull();
    expect(mocks.notices).toHaveLength(0);
  });

  it("says it will ask again next launch when the retry fails too", async () => {
    const phone = shell(null, { saveAttach: () => false });
    vi.stubGlobal("TstDeskNative", phone.bridge);
    mocks.redeem = async () => PAIRED;
    const conn = await load();
    await conn.connect();
    await conn.pairFromText(LINK);
    mocks.clients[0].sink.onStateChange?.("connected");

    const note = "This phone couldn't save the connection, so it will ask again next launch.";
    expect(conn.remoteAttach.storeNote).toBe(note);
    expect(mocks.notices).toEqual([{ key: "attach-unsaved", spec: expect.objectContaining({ body: note }) }]);
    // Once only: a later reconnect does not say it again.
    mocks.clients[0].sink.onStateChange?.("reconnecting");
    mocks.clients[0].sink.onStateChange?.("connected");
    expect(mocks.notices).toHaveLength(1);
  });
});

describe("a computer that can't read its paired devices (TD-5014 addendum 4)", () => {
  it("is not a refusal: the key stays, the client keeps retrying, the banner says why", async () => {
    const phone = shell(SAVED);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    const client = mocks.clients[0];
    client.sink.onStateChange?.("connecting");
    const message = "This computer can't read its paired devices right now. Trying again.";
    client.sink.onEvent?.({ type: "error", code: "devices_unavailable", message } as DaemonEventUnion);
    client.sink.onStateChange?.("reconnecting");

    expect(phone.state.saved).toBe(SAVED);
    expect(client.stopped).toBe(0);
    expect(conn.remoteAttach.needed).toBe(false);
    expect(conn.remoteAttach.note).toBe(message);

    client.sink.onStateChange?.("connected");
    expect(conn.remoteAttach.note).toBeNull();
  });
});

describe("a saved computer that stops answering (TD-5014)", () => {
  it("says so after a minute, and Pair again forgets nothing", async () => {
    vi.useFakeTimers();
    const phone = shell(SAVED);
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    const client = mocks.clients[0];
    client.sink.onStateChange?.("connecting");
    client.sink.onStateChange?.("reconnecting");
    vi.advanceTimersByTime(59_000);
    expect(conn.remoteAttach.unreachable).toBe(false);
    vi.advanceTimersByTime(1_000);
    expect(conn.remoteAttach.unreachable).toBe(true);

    conn.pairAgain();
    expect(conn.remoteAttach.needed).toBe(true);
    expect(conn.remoteAttach.fallback).toBe("ezer");
    expect(phone.state.saved).toBe(SAVED);
    expect(client.stopped).toBe(0);

    conn.useSavedTarget();
    expect(conn.remoteAttach.needed).toBe(false);
    client.sink.onStateChange?.("connected");
    expect(conn.remoteAttach.unreachable).toBe(false);
  });
});

describe("a saved connection the shell could not open (TD-5014)", () => {
  it("says so on the connect form once the retries are spent", async () => {
    vi.useFakeTimers();
    const phone = shell(null);
    phone.state.status = "unavailable";
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    expect(conn.remoteAttach.needed).toBe(true);
    expect(conn.remoteAttach.opening).toBe(true);
    expect(conn.remoteAttach.storeNote).toMatch(/key store/);

    vi.advanceTimersByTime(5_000);
    expect(conn.remoteAttach.opening).toBe(false);
    expect(conn.remoteAttach.canRetryStore).toBe(true);
    expect(mocks.clients).toHaveLength(0);
  });

  it("connects as soon as the key store answers", async () => {
    vi.useFakeTimers();
    const phone = shell(null);
    phone.state.status = "unavailable";
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();

    phone.state.saved = SAVED;
    phone.state.status = "ok";
    await vi.advanceTimersByTimeAsync(1_000);
    expect(conn.remoteAttach.needed).toBe(false);
    expect(conn.remoteAttach.opening).toBe(false);
    expect(conn.remoteAttach.storeNote).toBeNull();
    expect(conn.remoteAttach.desktopName).toBe("ezer");
    expect(mocks.clients[0].started).toBe(1);
  });

  it("tries again on Try again, and when the app comes back to the front", async () => {
    vi.useFakeTimers();
    const listeners = new Set<() => void>();
    vi.stubGlobal("document", {
      visibilityState: "visible",
      addEventListener: (_t: string, fn: () => void) => void listeners.add(fn),
      removeEventListener: (_t: string, fn: () => void) => void listeners.delete(fn),
    });
    const phone = shell(null);
    phone.state.status = "unavailable";
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();
    vi.advanceTimersByTime(5_000);

    conn.retrySavedConnection();
    expect(mocks.clients).toHaveLength(0);
    expect(conn.remoteAttach.canRetryStore).toBe(true);

    phone.state.saved = SAVED;
    phone.state.status = "ok";
    for (const fn of [...listeners]) fn();
    await vi.advanceTimersByTimeAsync(0);
    expect(conn.remoteAttach.needed).toBe(false);
    expect(mocks.clients[0].started).toBe(1);
    expect(listeners.size).toBe(0);
  });

  it("settles on the right note when the store answers with nothing", async () => {
    vi.useFakeTimers();
    const phone = shell(null);
    phone.state.status = "unavailable";
    vi.stubGlobal("TstDeskNative", phone.bridge);
    const conn = await load();
    await conn.connect();

    phone.state.status = "unreadable";
    vi.advanceTimersByTime(1_000);
    expect(conn.remoteAttach.opening).toBe(false);
    expect(conn.remoteAttach.canRetryStore).toBe(false);
    expect(conn.remoteAttach.storeNote).toMatch(/damaged/);
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
    mocks.redeem = async () => PAIRED;
    const conn = await load();
    await conn.connect();
    expect(conn.remoteAttach.canScan).toBe(false);
    expect(await conn.pairFromTyped("100.64.7.7:39079", "abcd-2345")).toBe(true);
    expect(mocks.redeemCalls[0]).toMatchObject({ deviceName: "Web browser" });
    expect(conn.remoteAttach.durable).toBe(false);
    expect([...session.values()][0]).toContain(NEW_KEY);
  });
});
