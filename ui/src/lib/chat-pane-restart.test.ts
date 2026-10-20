// @vitest-environment jsdom
//
// The chat pane after a restart (TD-5019), mounted for real: the draft comes
// back into the composer from localStorage, and a turn the daemon sealed as
// cut off shows "Cut off by a restart" with a Continue button that sends one
// ordinary message. Only the socket is faked.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
  handlers: new Set<(event: DaemonEventUnion) => void>(),
  sent: [] as ClientMessageUnion[],
}));

vi.mock("./connection-status.svelte.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
  return {
    ...real,
    onDaemonEvent: (handler: (event: DaemonEventUnion) => void) => {
      mocks.handlers.add(handler);
      return () => mocks.handlers.delete(handler);
    },
    sendToDaemon: (msg: ClientMessageUnion) => {
      mocks.sent.push(msg);
      return true;
    },
    attachToSession: () => {},
    detachFromSession: () => {},
  };
});

import ChatPane from "./components/chat/ChatPane.svelte";
import { ws } from "./connection-status.svelte.js";
import { draftKey } from "./draft-store";

let app: ReturnType<typeof mount> | null = null;
let data: Map<string, string>;

function emit(event: DaemonEventUnion): void {
  for (const handler of [...mocks.handlers]) handler(event);
  flushSync();
}

function sessionList(): DaemonEventUnion {
  return {
    type: "session_list",
    seq: 1,
    sessions: [
      {
        session_id: "s1",
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-10-14T10:00:00Z",
        updated_at: "2026-10-14T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  };
}

function userSends(): string[] {
  return mocks.sent.filter((m) => m.type === "user_message").map((m) => m.content);
}

beforeEach(() => {
  mocks.handlers.clear();
  mocks.sent.length = 0;
  data = new Map<string, string>();
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

describe("chat pane after a restart", () => {
  it("brings the saved draft back into the composer", () => {
    data.set(draftKey("s1"), JSON.stringify({ text: "where was I", queued: ["queued before the restart"] }));
    app = mount(ChatPane, { target: document.body });
    flushSync();
    emit(sessionList());

    const composer = document.querySelector("textarea");
    expect(composer?.value).toBe("queued before the restart\n\nwhere was I");
    expect(userSends()).toEqual([]);
  });

  it("saves what is typed in the composer once typing settles", async () => {
    app = mount(ChatPane, { target: document.body });
    flushSync();
    emit(sessionList());

    const composer = document.querySelector("textarea");
    if (composer === null) throw new Error("no composer");
    composer.value = "a thought in progress";
    composer.dispatchEvent(new Event("input", { bubbles: true }));
    flushSync();
    await new Promise((resolve) => setTimeout(resolve, 350));

    expect(JSON.parse(data.get(draftKey("s1")) ?? "null")).toEqual({ text: "a thought in progress", queued: [] });
  });

  it("offers Continue for a cut turn, and Continue sends one ordinary message", () => {
    app = mount(ChatPane, { target: document.body });
    flushSync();
    emit(sessionList());
    emit({ type: "user_turn", session_id: "s1", turn_id: "t1", content: "refactor the parser", seq: 2 });
    emit({ type: "assistant_delta", session_id: "s1", delta: "Reading parser.py", seq: 3 });
    emit({ type: "session_state", session_id: "s1", state: "running", seq: 4 });
    expect(document.body.textContent).not.toContain("Cut off by a restart");

    emit({
      type: "turn_complete",
      session_id: "s1",
      tokens: 0,
      cost: 0,
      tier: "brain",
      duration: 0,
      failed: true,
      error_code: "interrupted_by_restart",
      seq: 5,
    });

    const status = document.querySelector(".turn-status");
    expect(status?.textContent).toContain("Cut off by a restart");
    const button = [...(status?.querySelectorAll("button") ?? [])].find((b) => b.textContent?.trim() === "Continue");
    expect(button).toBeDefined();
    button?.click();
    flushSync();

    expect(userSends()).toEqual(["Continue"]);
    expect(status?.textContent).not.toContain("Cut off by a restart");
  });
});

describe("held messages after a restart (TD-5155)", () => {
  function heldList(messages: { queued_id: string; content: string; images_dropped: number }[]): DaemonEventUnion {
    return { type: "queued_messages", session_id: "s1", seq: 6, messages };
  }

  function strip(): HTMLElement | null {
    return document.querySelector<HTMLElement>("[aria-label='Held messages']");
  }

  it("shows what the daemon kept, and sends only on Send", () => {
    app = mount(ChatPane, { target: document.body });
    flushSync();
    emit(sessionList());
    expect(strip()).toBeNull();

    emit(
      heldList([
        { queued_id: "q1", content: "also check b.txt", images_dropped: 0 },
        { queued_id: "q2", content: "what is in this?", images_dropped: 1 },
      ]),
    );
    const rows = strip()?.querySelectorAll("[role='listitem']") ?? [];
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("also check b.txt");
    expect(rows[1].textContent).toContain("1 attached image was not kept");
    // Nothing goes out by itself: only the list request from the mount.
    const heldVerbs = () => mocks.sent.filter((m) => m.type === "send_queued" || m.type === "remove_queued");
    expect(heldVerbs()).toEqual([]);

    rows[0].querySelector<HTMLButtonElement>("[aria-label='Send held message']")?.click();
    flushSync();
    expect(heldVerbs()).toEqual([{ type: "send_queued", session_id: "s1", queued_id: "q1" }]);
    expect(userSends()).toEqual([]);
    expect(rows[0].querySelector<HTMLButtonElement>("[aria-label='Send held message']")?.disabled).toBe(true);

    rows[1].querySelector<HTMLButtonElement>("[aria-label='Remove held message']")?.click();
    flushSync();
    expect(heldVerbs().at(-1)).toEqual({ type: "remove_queued", session_id: "s1", queued_id: "q2" });

    emit(heldList([]));
    expect(strip()).toBeNull();
  });
});

