// Pins the webview content security policy (TD-4807). The policy lives in
// vite.config.ts (kit.csp, emitted as a build-time meta tag) rather than
// tauri.conf.json because hash mode must track the per-build bootstrap —
// see the comment above contentSecurityPolicy.

import { describe, it, expect } from "vitest";
import { contentSecurityPolicy } from "../vite.config";
import tauriConf from "../../shell/tauri.conf.json";

const prod = contentSecurityPolicy("production");
const dev = contentSecurityPolicy("development");
const prodD = prod.directives!;
const devD = dev.directives!;

describe("webview CSP (TD-4807)", () => {
  it("hashes inline scripts per build instead of allowlisting them", () => {
    expect(prod.mode).toBe("hash");
    expect(prodD["script-src"]).toEqual(["self"]);
  });

  it("permits only the daemon socket and Tauri IPC for connections", () => {
    expect(prodD["connect-src"]).toEqual([
      "self",
      "ws://127.0.0.1:*",
      "ipc://localhost",
    ]);
  });

  it("allows data: images for screen-frame previews, and nothing remote", () => {
    expect(prodD["img-src"]).toEqual(["self", "data:"]);
    expect(prodD["default-src"]).toEqual(["self"]);
  });

  it("locks down the directives that confine embedded content", () => {
    expect(prodD["object-src"]).toEqual(["none"]);
    expect(prodD["base-uri"]).toEqual(["none"]);
    expect(prodD["form-action"]).toEqual(["none"]);
    expect(prodD["font-src"]).toEqual(["self"]);
  });

  it("never permits eval or a remote script source in either mode", () => {
    for (const policy of [prod, dev]) {
      const all = Object.values(policy.directives!).flat();
      expect(all).not.toContain("unsafe-eval");
      expect(all).not.toContain("*");
      expect(all.filter((s) => s.startsWith("https:"))).toEqual([]);
    }
  });

  it("opens the Vite HMR socket in development only", () => {
    expect(devD["connect-src"]).toContain("ws://localhost:*");
    expect(prodD["connect-src"]).not.toContain("ws://localhost:*");
  });

  it("tauri.conf.json does not set a second, intersecting policy", () => {
    // Two CSPs both apply (intersection), and a static tauri.conf.json
    // script-src cannot carry the per-build bootstrap hash — the app
    // would fail to boot. The single policy stays in vite.config.ts.
    expect(tauriConf.app.security.csp).toBeNull();
  });
});
