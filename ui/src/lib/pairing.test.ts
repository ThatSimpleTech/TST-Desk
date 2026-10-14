import { describe, expect, it } from "vitest";
import type { SocketLike } from "./client";
import {
  attachStatusNote,
  bridgeDeviceName,
  canScanPairing,
  cleanDeviceName,
  formatCountdown,
  formatPairCode,
  installPairingHandlers,
  linkAddress,
  normalizePairCode,
  normalizeScanResult,
  pairedWords,
  pairLinkFromTyped,
  pairOfferCopy,
  parsePairLink,
  readAttachStatus,
  redeemPairing,
  refusedKeyMessage,
  relativeWords,
  replacedKey,
  scanFailureMessage,
  startPairingScan,
  takePendingPairLink,
  type ScanResult,
} from "./pairing";
import { nativeAttachBridge } from "./attach-store";

const KEY = "a".repeat(64);
const LINK = "tstdesk://pair?h=100.64.7.7&p=39079&c=ABCD2345&n=ezer";

describe("normalizePairCode (TD-5014)", () => {
  it("uppercases and drops spaces and dashes", () => {
    expect(normalizePairCode("abcd-2345")).toBe("ABCD2345");
    expect(normalizePairCode(" ab cd 23 45 ")).toBe("ABCD2345");
  });

  it("reads O as 0 and I or L as 1", () => {
    expect(normalizePairCode("O0IL-ABCD")).toBe("0011ABCD");
  });

  it("refuses the wrong length and letters outside the alphabet", () => {
    expect(normalizePairCode("ABCD234")).toBeNull();
    expect(normalizePairCode("ABCD23456")).toBeNull();
    expect(normalizePairCode("ABCD234U")).toBeNull();
    expect(normalizePairCode("ABCD234!")).toBeNull();
    expect(normalizePairCode("")).toBeNull();
  });

  it("formats the display form", () => {
    expect(formatPairCode("abcd2345")).toBe("ABCD-2345");
    expect(formatPairCode("nope")).toBe("nope");
  });
});

describe("parsePairLink (TD-5014)", () => {
  it("reads a good link", () => {
    expect(parsePairLink(LINK)).toEqual({
      ok: true,
      link: { host: "100.64.7.7", port: 39079, code: "ABCD2345", desktopName: "ezer" },
    });
  });

  it("decodes the name, normalizes the code, and allows no name", () => {
    const parsed = parsePairLink("TSTDESK://pair/?h=box.tail1234.ts.net&p=1&c=abcd-o0il&n=Adam%27s%20PC");
    expect(parsed).toEqual({
      ok: true,
      link: { host: "box.tail1234.ts.net", port: 1, code: "ABCD0011", desktopName: "Adam's PC" },
    });
    const bare = parsePairLink("tstdesk://pair?h=100.64.0.1&p=65535&c=ABCD2345");
    expect(bare.ok && bare.link.desktopName).toBeNull();
  });

  it("accepts a bracketed Tailscale IPv6 host", () => {
    const parsed = parsePairLink("tstdesk://pair?h=[fd7a:115c:a1e0::1]&p=39079&c=ABCD2345");
    expect(parsed.ok && parsed.link.host).toBe("fd7a:115c:a1e0::1");
  });

  it("refuses a host that is not on Tailscale", () => {
    for (const host of ["192.168.1.10", "127.0.0.1", "0.0.0.0", "example.com", "ts.net", "100.128.0.1"]) {
      const parsed = parsePairLink(`tstdesk://pair?h=${host}&p=39079&c=ABCD2345`);
      expect(parsed.ok, host).toBe(false);
    }
  });

  it("refuses bad ports, bad codes, and things that are not pair links", () => {
    expect(parsePairLink("tstdesk://pair?h=100.64.0.1&p=0&c=ABCD2345").ok).toBe(false);
    expect(parsePairLink("tstdesk://pair?h=100.64.0.1&p=65536&c=ABCD2345").ok).toBe(false);
    expect(parsePairLink("tstdesk://pair?h=100.64.0.1&p=12a&c=ABCD2345").ok).toBe(false);
    expect(parsePairLink("tstdesk://pair?h=100.64.0.1&p=1&c=ABCD").ok).toBe(false);
    expect(parsePairLink("tstdesk://pair?h=100.64.0.1&p=1").ok).toBe(false);
    expect(parsePairLink("tstdesk://other?h=100.64.0.1&p=1&c=ABCD2345").ok).toBe(false);
    expect(parsePairLink("https://pair?h=100.64.0.1&p=1&c=ABCD2345").ok).toBe(false);
    expect(parsePairLink("ABCD-2345").ok).toBe(false);
  });

  it("strips control characters and caps the name", () => {
    const parsed = parsePairLink(`tstdesk://pair?h=100.64.0.1&p=1&c=ABCD2345&n=${"x".repeat(80)}%0A`);
    expect(parsed.ok && parsed.link.desktopName).toBe("x".repeat(64));
  });

  it("sends typed address + code through the same rules", () => {
    expect(pairLinkFromTyped("100.64.7.7:39079", "abcd 2345")).toEqual({
      ok: true,
      link: { host: "100.64.7.7", port: 39079, code: "ABCD2345", desktopName: null },
    });
    expect(pairLinkFromTyped("ws://100.64.7.7:39079", "ABCD-2345").ok).toBe(true);
    expect(pairLinkFromTyped("192.168.1.10:39079", "ABCD2345").ok).toBe(false);
    expect(pairLinkFromTyped("100.64.7.7", "ABCD2345").ok).toBe(false);
    expect(pairLinkFromTyped("100.64.7.7:39079", "nope").ok).toBe(false);
    expect(pairLinkFromTyped("", "ABCD2345").ok).toBe(false);
  });
});

class FakeSocket implements SocketLike {
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {}
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
  }
  frame(value: unknown): void {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}

function harness() {
  const sockets: FakeSocket[] = [];
  const timers: Array<{ fn: () => void; ms: number; cleared: boolean }> = [];
  return {
    sockets,
    timers,
    opts: {
      host: "100.64.7.7",
      port: 39079,
      code: "ABCD2345",
      deviceName: "Pixel 11 Pro Fold",
      desktopName: "ezer",
      socketFactory: (url: string) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
      timers: {
        set: (fn: () => void, ms: number) => {
          const handle = { fn, ms, cleared: false };
          timers.push(handle);
          return handle;
        },
        clear: (handle: unknown) => {
          (handle as { cleared: boolean }).cleared = true;
        },
      },
    },
  };
}

describe("redeemPairing (TD-5014)", () => {
  it("sends the pair frame and resolves with this device's key", async () => {
    const h = harness();
    const pending = redeemPairing(h.opts);
    const socket = h.sockets[0];
    expect(socket.url).toBe("ws://100.64.7.7:39079");
    socket.onopen?.();
    expect(JSON.parse(socket.sent[0])).toEqual({
      type: "pair",
      code: "ABCD2345",
      device_name: "Pixel 11 Pro Fold",
      version: 1,
    });
    socket.frame({
      type: "pair_ok",
      version: 1,
      device_id: "dev_0123456789ab",
      device_name: "Pixel 11 Pro Fold",
      token: KEY,
      desktop_name: "ezer",
    });
    expect(await pending).toEqual({
      ok: true,
      target: { host: "100.64.7.7", port: 39079, token: KEY, deviceId: "dev_0123456789ab", desktopName: "ezer" },
    });
    expect(socket.closed).toBe(true);
    expect(h.timers[0].ms).toBe(10_000);
    expect(h.timers[0].cleared).toBe(true);
  });

  it("names the device key it replaces, and never a legacy token (TD-5014 addendum 1)", () => {
    const h = harness();
    void redeemPairing({ ...h.opts, replaces: KEY });
    h.sockets[0].onopen?.();
    expect(JSON.parse(h.sockets[0].sent[0])).toMatchObject({ type: "pair", replaces: KEY });

    for (const replaces of [null, undefined, "legacy-remote-token", "A".repeat(64)]) {
      const other = harness();
      void redeemPairing({ ...other.opts, replaces });
      other.sockets[0].onopen?.();
      expect(JSON.parse(other.sockets[0].sent[0])).not.toHaveProperty("replaces");
    }
  });

  it("passes the daemon's error frame through", async () => {
    const h = harness();
    const pending = redeemPairing(h.opts);
    h.sockets[0].onopen?.();
    h.sockets[0].frame({
      type: "error",
      code: "pair_locked",
      message: "Too many wrong codes. Start pairing again on the computer.",
    });
    expect(await pending).toEqual({
      ok: false,
      code: "pair_locked",
      message: "Too many wrong codes. Start pairing again on the computer.",
    });
    expect(h.sockets[0].closed).toBe(true);
  });

  it("gives up after the timeout and closes the socket", async () => {
    const h = harness();
    const pending = redeemPairing(h.opts);
    h.sockets[0].onopen?.();
    h.timers[0].fn();
    const result = await pending;
    expect(result.ok).toBe(false);
    expect(!result.ok && result.message).toContain("ezer");
    expect(h.sockets[0].closed).toBe(true);
  });

  it("reads a socket error or early close as a network failure", async () => {
    for (const fail of ["onerror", "onclose"] as const) {
      const h = harness();
      const pending = redeemPairing(h.opts);
      h.sockets[0][fail]?.();
      const result = await pending;
      expect(result.ok).toBe(false);
      expect(!result.ok && result.message).toMatch(/Couldn't reach ezer/);
    }
  });

  it("survives a socket constructor that throws", async () => {
    const h = harness();
    const result = await redeemPairing({
      ...h.opts,
      socketFactory: () => {
        throw new Error("blocked");
      },
    });
    expect(result.ok).toBe(false);
  });

  it("refuses a pair_ok whose key is the wrong shape", async () => {
    const h = harness();
    const pending = redeemPairing(h.opts);
    h.sockets[0].frame({ type: "pair_ok", version: 1, device_id: "dev_0123456789ab", token: "short", desktop_name: "ezer" });
    expect((await pending).ok).toBe(false);
  });

  it("ignores frames that are neither an answer nor JSON", async () => {
    const h = harness();
    const pending = redeemPairing(h.opts);
    h.sockets[0].onmessage?.({ data: "not json" });
    h.sockets[0].frame({ type: "ping" });
    h.sockets[0].frame({ type: "error", code: "pair_failed", message: "That pairing code didn't work. Start pairing again on the computer." });
    expect(await pending).toMatchObject({ ok: false, code: "pair_failed" });
  });
});

describe("device names (TD-5014)", () => {
  it("follows the daemon's device_name rule", () => {
    expect(cleanDeviceName("  Pixel\u0007 11  ")).toBe("Pixel 11");
    expect(cleanDeviceName("   ")).toBe("Unnamed device");
    expect(cleanDeviceName("y".repeat(70))).toHaveLength(64);
  });
});

function newShell(overrides: Record<string, unknown> = {}) {
  return {
    loadAttach: () => null,
    saveAttach: () => true,
    clearAttach: () => {},
    ...overrides,
  };
}

describe("shell bridge (TD-5014)", () => {
  it("works with a 0.1.5 shell that has none of the new methods", () => {
    const scope = { TstDeskNative: newShell() };
    expect(nativeAttachBridge(scope)).not.toBeNull();
    expect(canScanPairing(scope)).toBe(false);
    expect(startPairingScan(scope)).toBe(false);
    expect(takePendingPairLink(scope)).toBeNull();
    expect(readAttachStatus(scope)).toBeNull();
    expect(bridgeDeviceName(scope)).toBe("Web browser");
  });

  it("uses the new methods when the shell has them", () => {
    let pending: string | null = LINK;
    const scope = {
      TstDeskNative: newShell({
        deviceName: () => "Pixel 11 Pro Fold",
        scanPairing: () => true,
        takePairLink: () => {
          const link = pending;
          pending = null;
          return link;
        },
        attachStatus: () => "unreadable",
      }),
    };
    expect(canScanPairing(scope)).toBe(true);
    expect(startPairingScan(scope)).toBe(true);
    expect(bridgeDeviceName(scope)).toBe("Pixel 11 Pro Fold");
    expect(takePendingPairLink(scope)).toBe(LINK);
    expect(takePendingPairLink(scope)).toBeNull();
    expect(readAttachStatus(scope)).toBe("unreadable");
  });

  it("treats a throwing method or an unknown status as absent", () => {
    const scope = {
      TstDeskNative: newShell({
        scanPairing: () => {
          throw new Error("camera");
        },
        attachStatus: () => "weird",
      }),
    };
    expect(startPairingScan(scope)).toBe(false);
    expect(readAttachStatus(scope)).toBeNull();
  });

  it("has no bridge at all in a plain browser tab", () => {
    expect(canScanPairing({})).toBe(false);
    expect(takePendingPairLink({})).toBeNull();
  });

  it("explains a saved connection the shell could not open", () => {
    expect(attachStatusNote("unreadable")).toMatch(/damaged/);
    expect(attachStatusNote("unavailable")).toMatch(/still saved/);
    expect(attachStatusNote("ok")).toBeNull();
    expect(attachStatusNote("none")).toBeNull();
    expect(attachStatusNote(null)).toBeNull();
  });

  it("installs and removes the shell's callbacks", () => {
    const scope: Record<string, unknown> = {};
    const scans: ScanResult[] = [];
    const links: string[] = [];
    const off = installPairingHandlers({ onScan: (r) => scans.push(r), onPairLink: (l) => links.push(l) }, scope);
    (scope.__tstDeskOnScan as (raw: unknown) => void)({ ok: true, text: LINK });
    (scope.__tstDeskOnScan as (raw: unknown) => void)('{"ok":false,"reason":"permission"}');
    (scope.__tstDeskOnPairLink as (raw: unknown) => void)(LINK);
    (scope.__tstDeskOnPairLink as (raw: unknown) => void)(42);
    expect(scans).toEqual([
      { ok: true, text: LINK },
      { ok: false, reason: "permission" },
    ]);
    expect(links).toEqual([LINK]);
    off();
    expect(scope.__tstDeskOnScan).toBeUndefined();
    expect(scope.__tstDeskOnPairLink).toBeUndefined();
  });

  it("reads odd scan results as an error, and canceling as nothing to say", () => {
    expect(normalizeScanResult(null)).toEqual({ ok: false, reason: "error" });
    expect(normalizeScanResult("{")).toEqual({ ok: false, reason: "error" });
    expect(normalizeScanResult({ ok: false, reason: "lasers" })).toEqual({ ok: false, reason: "error" });
    expect(scanFailureMessage("canceled")).toBeNull();
    expect(scanFailureMessage("no_camera")).toMatch(/Type the code/);
  });
});

describe("re-pairing (TD-5014 addendum 1)", () => {
  const link = { host: "100.64.7.7", port: 39079, code: "ABCD2345", desktopName: "ezer" };
  const saved = { host: "100.64.7.7", port: 39079, token: KEY, deviceId: "dev_0123456789ab" };

  it("replaces the saved device key on the computer that issued it", () => {
    expect(replacedKey(saved, link)).toBe(KEY);
  });

  it("sends nothing for a legacy token, an empty record, or another computer", () => {
    expect(replacedKey({ host: saved.host, port: saved.port, token: "legacy-token" }, link)).toBeNull();
    expect(replacedKey(null, link)).toBeNull();
    expect(replacedKey({ ...saved, host: "100.64.0.9" }, link)).toBeNull();
    expect(replacedKey({ ...saved, port: 40000 }, link)).toBeNull();
  });
});

describe("a link from the shell (TD-5014)", () => {
  const link = { host: "100.64.7.7", port: 39079, code: "ABCD2345", desktopName: "ezer" };

  it("asks before pairing, and says what it replaces", () => {
    expect(pairOfferCopy(link, "office")).toEqual({
      question: "Pair with ezer at 100.64.7.7?",
      replaces: "This replaces office.",
      decline: "Back to office",
    });
  });

  it("has nothing to replace on a first pairing, and copes with no name", () => {
    expect(pairOfferCopy({ ...link, desktopName: null }, null)).toEqual({
      question: "Pair with the computer at 100.64.7.7?",
      replaces: null,
      decline: "Not now",
    });
  });

  it("fills the typed form's address, bracketing IPv6", () => {
    expect(linkAddress(link)).toBe("100.64.7.7:39079");
    expect(linkAddress({ ...link, host: "fd7a:115c:a1e0::1" })).toBe("[fd7a:115c:a1e0::1]:39079");
  });
});

describe("refusal copy (TD-5014)", () => {
  it("names the computer", () => {
    expect(refusedKeyMessage("ezer", true)).toBe(
      "This phone isn't paired with ezer anymore. Scan the code on the computer again.",
    );
    expect(refusedKeyMessage(null, true)).toContain("this computer");
    expect(refusedKeyMessage("ezer", false)).toMatch(/^This browser/);
  });
});

describe("list wording (TD-5014)", () => {
  const now = Date.parse("2026-10-14T12:00:00Z");
  it("says when a device was last seen in words", () => {
    expect(relativeWords("2026-10-14T11:59:30Z", now)).toBe("just now");
    expect(relativeWords("2026-10-14T11:59:00Z", now)).toBe("1 minute ago");
    expect(relativeWords("2026-10-14T11:15:00Z", now)).toBe("45 minutes ago");
    expect(relativeWords("2026-10-14T09:00:00Z", now)).toBe("3 hours ago");
    expect(relativeWords("2026-10-13T10:00:00Z", now)).toBe("yesterday");
    expect(relativeWords("2026-10-04T12:00:00Z", now)).toBe("10 days ago");
    expect(relativeWords("2026-07-14T12:00:00Z", now)).toBe("3 months ago");
    expect(relativeWords("2024-10-14T12:00:00Z", now)).toBe("over a year ago");
    expect(relativeWords("nonsense", now)).toBe("at an unknown time");
  });

  it("says when a device was paired, so two phones with one name differ", () => {
    const paired = new Date(2026, 9, 14, 10, 40).toISOString();
    expect(pairedWords(paired, now)).toBe("Paired Oct 14 at 10:40 AM");
    expect(pairedWords(new Date(2026, 9, 14, 22, 5).toISOString(), now)).toBe("Paired Oct 14 at 10:05 PM");
    expect(pairedWords(new Date(2025, 0, 2, 0, 7).toISOString(), now)).toBe("Paired Jan 2, 2025 at 12:07 AM");
    expect(pairedWords("nonsense", now)).toBe("Paired at an unknown time");
  });

  it("counts down without going negative", () => {
    expect(formatCountdown(600_000)).toBe("10:00");
    expect(formatCountdown(59_001)).toBe("1:00");
    expect(formatCountdown(5_000)).toBe("0:05");
    expect(formatCountdown(-1)).toBe("0:00");
  });
});
