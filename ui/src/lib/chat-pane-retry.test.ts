// @vitest-environment jsdom
//
// The chat pane's Working line while the daemon asks again for a reply it
// withdrew, mounted for real (TD-5159, TD-5233). A reply that spent the
// output limit thinking is retried with a larger limit, and the line says
// that instead of calling it interrupted. Only the socket is faked.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
  handlers: new Set<(event: DaemonEventUnion) => void>(),
}));

vi.mock("./connection-status.svelte.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
  return {
    ...real,
    onDaemonEvent: (handler: (event: DaemonEventUnion) => void) => {
      mocks.handlers.add(handler);
      return () => mocks.handlers.delete(handler);
    },
    sendToDaemon: (_msg: ClientMessageUnion) => true,
    attachToSession: () => {},
    detachFromSession: () => {},
  };
});

import ChatPane from "./components/chat/ChatPane.svelte";
import { ws } from "./connection-status.svelte.js";

let app: ReturnType<typeof mount> | null = null;
let seq = 1;

function emit(event: Record<string, unknown>): void {
  seq += 1;
  const full = { session_id: "s1", seq, ...event } as DaemonEventUnion;
  for (const handler of [...mocks.handlers]) handler(full);
  flushSync();
}

function status(): string {
  return document.querySelector(".turn-status")?.textContent ?? "";
}

function startTurn(): void {
  app = mount(ChatPane, { target: document.body });
  flushSync();
  emit({
    type: "session_list",
    sessions: [
      {
        session_id: "s1",
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-11-04T10:00:00Z",
        updated_at: "2026-11-04T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  });
  emit({ type: "user_turn", turn_id: "t1", content: "plan the migration" });
}

beforeEach(() => {
  mocks.handlers.clear();
  const data = new Map<string, string>();
  const storage: Storage = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
    clear: () => data.clear(),
    key: (index) => [...data.keys()][index] ?? null,
    get length() {
      return data.size;
    },
  };
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
  if (typeof globalThis.ResizeObserver === "undefined") {
    globalThis.ResizeObserver = class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    } as unknown as typeof ResizeObserver;
  }
  ws.state = "connected";
});

afterEach(() => {
  if (app !== null) {
    unmount(app);
    app = null;
  }
  ws.state = "disconnected";
});

describe("the Working line while a withdrawn reply is asked again", () => {
  it("says the model ran out of room to think when thinking used the output limit", () => {
    startTurn();
    emit({ type: "assistant_reasoning", delta: "Weighing every option at length", response_id: "r1" });
    emit({ type: "assistant_retract", response_id: "r1", reason: "output_cap_thinking" });

    expect(status()).toContain("Out of room to think. Asking again with a larger limit…");
    expect(status()).not.toContain("Reply interrupted");

    // The retry's first thinking token ends the wait; the line goes with it.
    emit({ type: "assistant_reasoning", delta: "Shorter.", response_id: "r2" });
    expect(status()).not.toContain("Out of room to think");
  });

  it("still says the reply was interrupted when the provider went quiet", () => {
    startTurn();
    emit({ type: "assistant_delta", delta: "The first step is", response_id: "r1" });
    emit({ type: "assistant_retract", response_id: "r1", reason: "stream_stalled" });

    expect(status()).toContain("Reply interrupted. Asking again…");
    expect(status()).not.toContain("Out of room to think");
  });
});
