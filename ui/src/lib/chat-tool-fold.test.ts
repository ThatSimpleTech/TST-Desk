// @vitest-environment jsdom
//
// The fold above a tool call, fed the daemon's events through the app's
// own $state store and mounted in the bubble the chat shows (TD-1902,
// TD-5144). The case that matters: a restart that cuts a call off seals
// the turn with turn_complete and never logs a tool_result, so the call's
// status stays unset for good. The fold must stop shimmering then, for
// every tool and in both of the bubble's loops. Only the shell's invoke
// and the socket are faked.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const tauri = vi.hoisted(() => ({ invoke: vi.fn(async () => null) }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: tauri.invoke }));
vi.mock("./machines.svelte.js", () => ({ machineNameFor: () => null }));
const socket = vi.hoisted(() => ({ handlers: new Set<(event: unknown) => void>() }));
vi.mock("./connection-status.svelte.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
  return {
    ...real,
    onDaemonEvent: (handler: (event: unknown) => void) => {
      socket.handlers.add(handler);
      return () => socket.handlers.delete(handler);
    },
    onConnectionState: () => () => {},
    onResume: () => () => {},
    sendToDaemon: () => true,
    attachToSession: () => {},
    detachFromSession: () => {},
  };
});

import type { ChatMessage } from "./chat-store";
import { chat, initChat, teardownChat } from "./chat-store.svelte.js";
import MessageBubble from "./components/chat/MessageBubble.svelte";
import type { DaemonEventUnion } from "./protocol";
import { resetDisclosures } from "./reasoning-disclosure.svelte.js";

const SID = "sess-fold";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
  for (const handler of [...socket.handlers]) handler(event);
  flushSync();
}

function bindTo(sessionId: string): void {
  emit({
    type: "session_list",
    seq: 1,
    sessions: [
      {
        session_id: sessionId,
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-10-14T10:00:00Z",
        updated_at: "2026-10-14T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  } as DaemonEventUnion);
}

const userTurn = { type: "user_turn", seq: 18, session_id: SID, turn_id: "turn-1", content: "Look at the build" } as DaemonEventUnion;

function call(name: string): DaemonEventUnion {
  const args = name === "send_file" ? { files: ["build/app.apk"] } : { path: "README.md" };
  return {
    type: "tool_call",
    seq: 20,
    session_id: SID,
    tool_call_id: "call_cut",
    name,
    arguments: args,
    decision_class: "A",
  } as DaemonEventUnion;
}

/** What the daemon logs when a restart cut the turn: no tool_result. */
const cutByRestart = {
  type: "turn_complete",
  seq: 21,
  session_id: SID,
  tokens: 0,
  cost: 0,
  tier: "worker",
  duration: 4.2,
  failed: true,
  error_code: "interrupted_by_restart",
} as DaemonEventUnion;

function assistantRow(): ChatMessage {
  const row = chat.messages.find((m) => m.role === "assistant");
  if (row === undefined) throw new Error("no assistant row");
  return row;
}

type Loop = "parts" | "tools";

/** Mount the bubble on the store's row. "tools" drops the ordered parts so
 *  the bubble takes its fallback loop over the flat tool list. */
function mountBubble(loop: Loop): void {
  const row = assistantRow();
  const message = loop === "parts" ? row : { ...row, parts: undefined };
  app = mount(MessageBubble, { target: document.body, props: { message } });
  flushSync();
}

function summary(): HTMLButtonElement {
  const found = document.querySelector<HTMLButtonElement>(".fold .summary");
  if (found === null) throw new Error("no tool fold");
  return found;
}

function shimmers(): boolean {
  return document.querySelector(".fold.live") !== null || document.querySelector(".shimmer") !== null;
}

beforeEach(() => {
  initChat();
  bindTo(SID);
});

afterEach(() => {
  if (app !== null) {
    unmount(app);
    app = null;
  }
  teardownChat();
  resetDisclosures();
  document.body.innerHTML = "";
});

describe.each(["fs_read", "send_file"])("the %s fold, from the store", (name) => {
  describe.each<Loop>(["parts", "tools"])("in the %s loop", (loop) => {
    it("shimmers and stays open while the call runs", () => {
      emit(userTurn);
      emit(call(name));
      mountBubble(loop);
      expect(shimmers()).toBe(true);
      expect(summary().getAttribute("aria-expanded")).toBe("true");
    });

    it("stops shimmering and folds once a restart cuts the call off", () => {
      emit(userTurn);
      emit(call(name));
      emit(cutByRestart);
      expect(assistantRow().complete).toBe(true);
      expect(assistantRow().tools?.[0].status).toBeUndefined();
      mountBubble(loop);
      expect(shimmers()).toBe(false);
      expect(summary().getAttribute("aria-expanded")).toBe("false");
    });

    it("opens on the first click once cut off", () => {
      for (const event of [userTurn, call(name), cutByRestart]) emit(event);
      mountBubble(loop);
      summary().click();
      flushSync();
      expect(summary().getAttribute("aria-expanded")).toBe("true");
    });
  });

  it("stops shimmering on a mounted row when the cut turn seals", () => {
    emit(userTurn);
    emit(call(name));
    mountBubble("parts");
    expect(shimmers()).toBe(true);
    emit(cutByRestart);
    expect(shimmers()).toBe(false);
    expect(summary().getAttribute("aria-expanded")).toBe("false");
  });
});
