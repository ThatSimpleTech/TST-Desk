// @vitest-environment jsdom
//
// HOST-01: Approve and Deny in the OS notification did nothing in every
// shipped build. The window registered the host's approval-action listener
// only when no bridge was injected, and inside Tauri one always is. This
// runs as the packaged app does: Tauri present, the real bridge injected.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tauri = vi.hoisted(() => ({
  handlers: new Map<string, (event: { payload: unknown }) => void>(),
  unlistened: [] as string[],
  label: "main",
  sent: [] as Array<Record<string, unknown>>,
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: async (name: string, handler: (event: { payload: unknown }) => void) => {
    tauri.handlers.set(name, handler);
    return () => {
      tauri.unlistened.push(name);
      tauri.handlers.delete(name);
    };
  },
}));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ label: tauri.label, setFocus: async () => {} }),
}));
vi.mock("@tauri-apps/plugin-notification", () => ({ onAction: async () => ({}) }));
vi.mock("./connection-status.svelte.js", () => ({
  onEvent: () => () => {},
  sendToDaemon: (msg: Record<string, unknown>) => {
    tauri.sent.push(msg);
    return true;
  },
}));

import { createTauriOsNotifyBridge, resetOsNotify, startOsNotify } from "./os-notify.svelte.js";

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  tauri.handlers.clear();
  tauri.unlistened.length = 0;
  tauri.sent.length = 0;
  tauri.label = "main";
  resetOsNotify();
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

describe("the notification's buttons", () => {
  it("Approve sends the approve frame with the real bridge injected", async () => {
    const stop = startOsNotify(createTauriOsNotifyBridge());
    await settle();
    const handler = tauri.handlers.get("approval-action");
    expect(handler).toBeDefined();
    handler!({ payload: { session_id: "s1", tool_call_id: "tc-1", action: "approve" } });
    expect(tauri.sent).toHaveLength(1);
    expect(tauri.sent[0]).toMatchObject({ type: "approve", session_id: "s1", tool_call_id: "tc-1" });
    stop();
    expect(tauri.unlistened).toContain("approval-action");
  });

  it("Deny sends deny; Show sends nothing", async () => {
    startOsNotify(createTauriOsNotifyBridge());
    await settle();
    const handler = tauri.handlers.get("approval-action")!;
    handler({ payload: { session_id: "s1", tool_call_id: "tc-2", action: "deny" } });
    handler({ payload: { session_id: "s1", tool_call_id: "tc-2", action: "show" } });
    expect(tauri.sent.map((m) => m.type)).toEqual(["deny"]);
  });

  it("only the main window answers, so a session window does not answer twice", async () => {
    tauri.label = "session-s1";
    startOsNotify(createTauriOsNotifyBridge());
    await settle();
    expect(tauri.handlers.has("approval-action")).toBe(false);
  });

  it("outside the shell nothing listens", async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    startOsNotify();
    await settle();
    expect(tauri.handlers.size).toBe(0);
  });
});
