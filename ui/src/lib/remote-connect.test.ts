import { describe, expect, it } from "vitest";
import {
  daemonWsUrl,
  isRemoteShellHost,
  isUnspecifiedHost,
  parseAttachParams,
  parseStoredAttach,
  parseWsUrl,
  resolveBrowserTarget,
  serializeAttach,
  stripAttachFromUrl,
  validateAttachTarget,
  wsHostPolicyFor,
} from "./remote-connect";

describe("parseWsUrl", () => {
  it("accepts ws://host:port and a bare host:port", () => {
    expect(parseWsUrl("ws://100.64.1.2:9000")).toEqual({
      ok: true,
      host: "100.64.1.2",
      port: 9000,
    });
    expect(parseWsUrl("100.64.1.2:9000")).toEqual({
      ok: true,
      host: "100.64.1.2",
      port: 9000,
    });
  });

  it("accepts a Tailscale IPv6 URL", () => {
    expect(parseWsUrl("ws://[fd7a:115c:a1e0::1]:9000")).toEqual({
      ok: true,
      host: "fd7a:115c:a1e0::1",
      port: 9000,
    });
  });

  it("refuses unspecified binds and TLS", () => {
    expect(parseWsUrl("ws://0.0.0.0:9000")).toEqual({
      ok: false,
      error: "Refusing 0.0.0.0 / :: — connect to a specific address.",
    });
    expect(parseWsUrl("ws://[::]:9000")).toEqual({
      ok: false,
      error: "Refusing 0.0.0.0 / :: — connect to a specific address.",
    });
    expect(parseWsUrl("wss://100.64.1.2:9000").ok).toBe(false);
    expect(parseWsUrl("http://100.64.1.2:9000").ok).toBe(false);
  });

  it("requires a port", () => {
    expect(parseWsUrl("ws://100.64.1.2").ok).toBe(false);
  });

  it("desktop policy still accepts a specific non-tailscale host", () => {
    expect(wsHostPolicyFor("production")).toBe("any-specific");
    expect(wsHostPolicyFor("development")).toBe("any-specific");
    expect(parseWsUrl("ws://192.168.1.9:9", "any-specific").ok).toBe(true);
    expect(parseWsUrl("ws://127.0.0.1:9", "any-specific").ok).toBe(true);
  });
});

describe("remote-shell host policy", () => {
  it("is the android build's policy", () => {
    expect(wsHostPolicyFor("android")).toBe("remote-shell");
  });

  it("accepts Tailscale CGNAT, ULA, and MagicDNS", () => {
    expect(isRemoteShellHost("100.64.0.1")).toBe(true);
    expect(isRemoteShellHost("100.127.255.255")).toBe(true);
    expect(isRemoteShellHost("fd7a:115c:a1e0::1")).toBe(true);
    expect(isRemoteShellHost("FD7A:115C:A1E0::1")).toBe(true);
    expect(isRemoteShellHost("desk.tailnet.ts.net")).toBe(true);
    for (const url of [
      "ws://100.64.0.1:9",
      "ws://100.127.255.255:9",
      "ws://[fd7a:115c:a1e0::1]:9",
      "ws://desk.tailnet.ts.net:9",
    ]) {
      expect(parseWsUrl(url, "remote-shell").ok).toBe(true);
    }
    expect(validateAttachTarget("ws://100.64.1.2:9", "tok", "remote-shell").ok).toBe(true);
  });

  it("refuses unspecified, loopback, LAN, and public hosts", () => {
    expect(isRemoteShellHost("100.63.255.255")).toBe(false);
    expect(isRemoteShellHost("100.128.0.1")).toBe(false);
    expect(isRemoteShellHost("fd7a:115c:a1e1::1")).toBe(false);
    expect(isRemoteShellHost("ts.net")).toBe(false);
    expect(isRemoteShellHost("evil.ts.net.example")).toBe(false);
    for (const url of [
      "ws://0.0.0.0:9",
      "ws://[::]:9",
      "ws://127.0.0.1:9",
      "ws://localhost:9",
      "ws://10.1.1.1:9",
      "ws://192.168.1.9:9",
      "ws://8.8.8.8:9",
      "ws://example.com:9",
      "ws://ts.net:9",
      "ws://[fd7a:115c:a1e1::1]:9",
    ]) {
      expect(parseWsUrl(url, "remote-shell").ok).toBe(false);
    }
    const refused = validateAttachTarget("ws://8.8.8.8:9", "tok", "remote-shell");
    expect(refused.ok).toBe(false);
    if (!refused.ok && refused.reason === "invalid") {
      expect(refused.error).toContain("Tailscale");
    }
  });
});

describe("daemonWsUrl", () => {
  it("leaves IPv4 bare and brackets IPv6", () => {
    expect(daemonWsUrl("100.64.1.2", 9000)).toBe("ws://100.64.1.2:9000");
    expect(daemonWsUrl("fd7a:115c:a1e0::1", 9000)).toBe("ws://[fd7a:115c:a1e0::1]:9000");
    expect(daemonWsUrl("[fd7a:115c:a1e0::1]", 9000)).toBe("ws://[fd7a:115c:a1e0::1]:9000");
  });
});

describe("validateAttachTarget", () => {
  it("requires a non-empty token", () => {
    const missing = validateAttachTarget("ws://100.64.1.2:9000", "  ");
    expect(missing.ok).toBe(false);
    const ok = validateAttachTarget("ws://100.64.1.2:9000", " abcd ");
    expect(ok).toEqual({
      ok: true,
      target: { host: "100.64.1.2", port: 9000, token: "abcd" },
    });
  });
});

describe("parseAttachParams", () => {
  it("prefers the hash over the query so a token is not an HTTP query", () => {
    const parsed = parseAttachParams(
      "?ws=ws://127.0.0.1:1&token=query",
      "#ws=ws://100.64.1.2:9000&token=remote",
    );
    expect(parsed).toEqual({
      ok: true,
      target: { host: "100.64.1.2", port: 9000, token: "remote" },
    });
  });

  it("reads the query when the hash is empty", () => {
    const parsed = parseAttachParams("?ws=ws://100.64.1.2:9000&token=remote");
    expect(parsed.ok && parsed.target.token).toBe("remote");
  });

  it("is missing when neither side has attach params", () => {
    expect(parseAttachParams("", "")).toEqual({ ok: false, reason: "missing" });
  });
});

describe("resolveBrowserTarget", () => {
  it("uses the URL, then a stored target, and otherwise asks", () => {
    const fromUrl = resolveBrowserTarget({
      search: "?ws=ws://100.64.1.2:9000&token=t",
      hash: "",
      stored: null,
    });
    expect(fromUrl).toMatchObject({ ok: true, from: "url", stripUrl: true });

    const stored = resolveBrowserTarget({
      search: "",
      hash: "",
      stored: { host: "100.64.1.2", port: 9000, token: "t" },
    });
    expect(stored).toMatchObject({ ok: true, from: "stored", stripUrl: false });

    expect(resolveBrowserTarget({ search: "", hash: "", stored: null })).toEqual({
      ok: false,
      error: null,
    });
  });

  it("surfaces a malformed URL instead of falling back to storage", () => {
    const bad = resolveBrowserTarget({
      search: "?ws=ws://0.0.0.0:9000&token=t",
      hash: "",
      stored: { host: "100.64.1.2", port: 9000, token: "t" },
    });
    expect(bad.ok).toBe(false);
    if (!bad.ok) expect(bad.error).toMatch(/0\.0\.0\.0/);
  });
});

describe("stripAttachFromUrl", () => {
  it("drops ws and token from the query and hash", () => {
    expect(stripAttachFromUrl("http://ui.test/?ws=ws://h:1&token=secret&x=1")).toBe("/?x=1");
    expect(stripAttachFromUrl("http://ui.test/#ws=ws://h:1&token=secret")).toBe("/");
  });
});

describe("stored attach", () => {
  it("round-trips a valid target and rejects junk", () => {
    const target = { host: "100.64.1.2", port: 9000, token: "remote" };
    expect(parseStoredAttach(serializeAttach(target))).toEqual(target);
    expect(parseStoredAttach("{")).toBeNull();
    expect(parseStoredAttach(JSON.stringify({ host: "0.0.0.0", port: 1, token: "x" }))).toBeNull();
  });
});

describe("isUnspecifiedHost", () => {
  it("names the binds the daemon also refuses", () => {
    expect(isUnspecifiedHost("0.0.0.0")).toBe(true);
    expect(isUnspecifiedHost("::")).toBe(true);
    expect(isUnspecifiedHost("100.64.1.2")).toBe(false);
    expect(isUnspecifiedHost("127.0.0.1")).toBe(false);
  });
});
