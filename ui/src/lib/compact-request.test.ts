// @vitest-environment jsdom
//
// TD-5154: every way to compact (/compact, "Summarize from here", the
// gauge's Compact now) goes through requestCompact, which says why when the
// request never left the window. Before, /compact on another computer's
// session said "A turn is running" while nothing ran, and the two buttons did
// nothing at all, because the send gate dropped compact_context for bridged
// ids. The socket is faked; the gate is the real one.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const socket = vi.hoisted(() => ({
  connected: true,
  sent: [] as Array<Record<string, unknown>>,
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => null) }));
vi.mock("./machines.svelte.js", () => ({ machineNameFor: () => null }));
vi.mock("./connection-status.svelte.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
  const { refusedForBridged } = await import("./bridged-id");
  return {
    ...real,
    onDaemonEvent: () => () => {},
    onConnectionState: () => () => {},
    onResume: () => () => {},
    // The real gate, then a socket that may be down.
    sendToDaemon: (msg: import("./protocol").ClientMessageUnion) => {
      if (refusedForBridged(msg) || !socket.connected) return false;
      socket.sent.push(msg as unknown as Record<string, unknown>);
      return true;
    },
    attachToSession: () => {},
    detachFromSession: () => {},
  };
});

import { chat, requestCompact, selectSession } from "./chat-store.svelte.js";
import { clearNotifications, toasts } from "./notifications.svelte.js";

const OWN = "3f2a9c1e-7b4d-4e8a-9f01-2c3d4e5f6a7b";
const BRIDGED = `${OWN}:mch_0123456789ab`;

/** Bind the pane to *id*, with a turn live or not. A bind never raises
 *  "running" by itself (TD-1714), so the turn is set as a delta would. */
function bind(id: string, turn: "idle" | "running"): void {
  selectSession(id, "idle");
  chat.turnState = turn;
}

beforeEach(() => {
  socket.connected = true;
  socket.sent.length = 0;
  clearNotifications();
});

afterEach(() => {
  clearNotifications();
});

describe("requestCompact", () => {
  it("reaches the hub for a session on another computer", () => {
    bind(BRIDGED, "idle");
    expect(requestCompact("keep the parser")).toBe(true);
    expect(requestCompact("", 2)).toBe(true);
    expect(socket.sent).toEqual([
      { type: "compact_context", session_id: BRIDGED, guidance: "keep the parser" },
      { type: "compact_context", session_id: BRIDGED, user_index: 2 },
    ]);
    expect(toasts).toHaveLength(0);
  });

  it("says a turn is running only when one is", () => {
    bind(OWN, "running");
    expect(requestCompact()).toBe(false);
    expect(socket.sent).toEqual([]);
    expect(toasts.map((t) => [t.title, t.body])).toEqual([
      ["Not compacted", "A turn is running. Compact when it finishes, or stop it first."],
    ]);
  });

  it("says the connection is down when nothing could carry it", () => {
    socket.connected = false;
    for (const id of [OWN, BRIDGED]) {
      clearNotifications();
      bind(id, "idle");
      expect(requestCompact("", 1)).toBe(false);
      expect(toasts).toHaveLength(1);
      expect(toasts[0].body).toContain("isn't connected");
      expect(toasts[0].body).not.toContain("turn is running");
    }
  });
});
