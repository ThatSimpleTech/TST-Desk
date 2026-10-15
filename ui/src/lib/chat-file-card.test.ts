// @vitest-environment jsdom
//
// The file cards a send_file call puts in the chat (TD-5144, design §6.3,
// §6.4), mounted for real on the desktop: the skeleton while the call runs,
// the card's lines, the buttons for the computer that holds the file, and
// no card for a refused call. Only the shell's invoke and the socket are
// faked.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const tauri = vi.hoisted(() => ({ invoke: vi.fn() }));
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

import type { ToolBlock } from "./chat-store";
import { chat, initChat, selectSession, teardownChat } from "./chat-store.svelte.js";
import FileCards from "./components/chat/FileCards.svelte";
import MessageBubble from "./components/chat/MessageBubble.svelte";
import { configureDownloads, resetDownloads } from "./downloads.svelte";
import { createTauriHost } from "./file-sink";
import type { ArtifactReady, DaemonEventUnion } from "./protocol";
import fixtures from "./protocol-fixtures.json";
import type { SentFileRef } from "./sent-files";

const APK: SentFileRef = {
  artifactId: "1".repeat(32),
  sessionId: "sess-1",
  name: "app-release.apk",
  size: 46_200_000,
  mime: "application/vnd.android.package-archive",
  sha256: "ab".repeat(32),
  caption: "0.1.9 release build",
  toolCallId: "call_abc",
};

const NOTES: SentFileRef = {
  ...APK,
  artifactId: "2".repeat(32),
  name: "notes.pdf",
  size: 1200,
  mime: "application/pdf",
};

const SETUP: SentFileRef = {
  ...APK,
  artifactId: "3".repeat(32),
  name: "setup.exe",
  size: 2_000_000,
  mime: "application/octet-stream",
  caption: null,
};

let app: ReturnType<typeof mount> | null = null;
/** What sent_file_status answers, by artifact id. */
let onDisk: Record<string, "local" | "none">;

function block(over: Partial<ToolBlock> = {}): ToolBlock {
  return {
    toolCallId: "call_abc",
    name: "send_file",
    arguments: { files: ["android/app/build/outputs/apk/release/app-release.apk"], caption: "0.1.9 release build" },
    ...over,
  };
}

async function render(tool: ToolBlock): Promise<void> {
  app = mount(FileCards, { target: document.body, props: { block: tool } });
  flushSync();
  // The first render shows "checking" until the shell's status answers.
  await vi.waitFor(() => {
    flushSync();
    expect(document.querySelector('.file-card[data-phase="checking"]')).toBeNull();
  });
}

function buttons(): string[] {
  return [...document.querySelectorAll("button")].map((b) => b.textContent?.trim() ?? "");
}

function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
  if (found === undefined) throw new Error(`no ${label} button in ${JSON.stringify(buttons())}`);
  return found;
}

beforeEach(() => {
  onDisk = {};
  tauri.invoke.mockReset();
  tauri.invoke.mockImplementation(async (cmd: string, args: { artifactId: string }) => {
    if (cmd === "sent_file_status") {
      const state = onDisk[args.artifactId] ?? "none";
      return { state, have: 0 };
    }
    return null;
  });
  resetDownloads();
  configureDownloads({ platform: "desktop", host: createTauriHost(), send: () => true, machine: () => "this computer" });
});

afterEach(() => {
  if (app !== null) {
    unmount(app);
    app = null;
  }
  resetDownloads();
  document.body.innerHTML = "";
});

describe("file cards in the chat", () => {
  it("shows one skeleton naming the first file while the call runs", async () => {
    await render(block());
    const skeleton = document.querySelector('[role="status"]');
    expect(skeleton?.textContent?.trim()).toBe("Sending app-release.apk…");
    expect(document.querySelector(".file-card")).toBeNull();
  });

  it("shows the name and the kind and size line", async () => {
    await render(block({ status: "success", files: [APK] }));
    const card = document.querySelector(".file-card");
    expect(card?.getAttribute("aria-label")).toBe("File: app-release.apk, 46.2 MB");
    expect(card?.querySelector(".name")?.getAttribute("title")).toBe("app-release.apk");
    expect(card?.querySelector(".name")?.textContent).toBe("app-release.apk");
    expect(card?.querySelector(".status")?.textContent).toBe("Android app · 46.2 MB");
    expect(card?.querySelector(".status")?.getAttribute("aria-live")).toBe("polite");
    expect(buttons()).toEqual(["Download"]);
  });

  it("on the computer that has it: Open, with Show in folder and Save a copy in the menu", async () => {
    onDisk[NOTES.artifactId] = "local";
    await render(block({ status: "success", files: [NOTES] }));
    expect(document.querySelector(".file-card")?.getAttribute("data-phase")).toBe("local");

    button("Open").click();
    await vi.waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("sent_file_open", { sessionId: "sess-1", artifactId: NOTES.artifactId }),
    );

    const more = document.querySelector<HTMLButtonElement>('button[aria-label="More for notes.pdf"]');
    more?.click();
    flushSync();
    const items = [...document.querySelectorAll('[role="menuitem"]')].map((b) => b.textContent?.trim());
    expect(items).toEqual(["Show in folder", "Save a copy…"]);
  });

  it("never offers Open for a program", async () => {
    onDisk[SETUP.artifactId] = "local";
    await render(block({ status: "success", files: [SETUP] }));
    document.querySelector<HTMLButtonElement>('button[aria-label="More for setup.exe"]')?.click();
    flushSync();
    expect(buttons()).not.toContain("Open");
    expect(buttons()).toContain("Show in folder");
    expect(buttons()).toContain("Save a copy…");

    button("Show in folder").click();
    await vi.waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("sent_file_reveal", { sessionId: "sess-1", artifactId: SETUP.artifactId }),
    );
    expect(tauri.invoke).not.toHaveBeenCalledWith("sent_file_open", expect.anything());
  });

  it("shows no card for a refused call", async () => {
    await render(block({ status: "error", files: [APK] }));
    expect(document.querySelector(".file-card")).toBeNull();
    expect(document.querySelector('[role="status"]')).toBeNull();
  });

  it("shows the caption once for two files", async () => {
    await render(block({ status: "success", files: [APK, NOTES] }));
    expect(document.querySelectorAll(".file-card")).toHaveLength(2);
    expect(document.body.textContent?.split("0.1.9 release build")).toHaveLength(2);
  });
});

// ── Through the real store (finding 1) ──────────────────────────────────
//
// The cards above get a hand-built block. These feed the daemon's events
// through the app's own $state store and mount the bubble the chat shows,
// so a card the reducer writes where the bubble does not read fails here.

const SENT = fixtures.artifact_ready as ArtifactReady;
const SID = SENT.session_id;

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

const userTurn = { type: "user_turn", seq: 18, session_id: SID, turn_id: "turn-1", content: "Send me the build" } as DaemonEventUnion;
const delta = { type: "assistant_delta", seq: 19, session_id: SID, delta: "Here it is." } as DaemonEventUnion;
const call = {
  type: "tool_call",
  seq: 20,
  session_id: SID,
  tool_call_id: SENT.tool_call_id,
  name: "send_file",
  arguments: { files: ["android/app/build/outputs/apk/release/app-release.apk"], caption: "0.1.9 release build" },
  decision_class: "A",
} as DaemonEventUnion;
const result = {
  type: "tool_result",
  seq: 23,
  session_id: SID,
  tool_call_id: SENT.tool_call_id,
  status: "success",
  output: "Sent app-release.apk (2 B) to the chat.",
  truncated: false,
  error_code: null,
  diff: null,
} as DaemonEventUnion;
const done = {
  type: "turn_complete",
  seq: 24,
  session_id: SID,
  tokens: 10,
  cost: 0,
  tier: "worker",
  duration: 1.5,
  failed: false,
  error_code: null,
} as DaemonEventUnion;

function assistantRow() {
  const row = chat.messages.find((m) => m.role === "assistant");
  if (row === undefined) throw new Error("no assistant row");
  return row;
}

function mountBubble(): void {
  app = mount(MessageBubble, { target: document.body, props: { message: assistantRow() } });
  flushSync();
}

function cardsNamed(name: string): Element[] {
  return [...document.querySelectorAll(".file-card")].filter(
    (card) => card.querySelector(".name")?.textContent === name,
  );
}

async function expectOneCard(name: string): Promise<void> {
  await vi.waitFor(() => {
    flushSync();
    expect(cardsNamed(name)).toHaveLength(1);
  });
  expect(document.querySelectorAll(".file-card")).toHaveLength(1);
  expect(document.querySelector(".skeleton")).toBeNull();
}

describe("a send_file card in the chat, from the store", () => {
  beforeEach(() => {
    initChat();
    bindTo(SID);
  });

  afterEach(() => {
    teardownChat();
  });

  it("lands under the call live: skeleton first, then the card", async () => {
    emit(userTurn);
    emit(call);
    mountBubble();
    expect(document.querySelector(".skeleton")?.textContent?.trim()).toBe("Sending app-release.apk…");
    emit(SENT);
    emit(result);
    emit(done);
    await expectOneCard("app-release.apk");
  });

  it("lands under the call when text came before it", async () => {
    emit(userTurn);
    emit(delta);
    emit(call);
    mountBubble();
    emit(SENT);
    emit(result);
    await expectOneCard("app-release.apk");
    expect(assistantRow().tools?.[0].files).toHaveLength(1);
  });

  it("comes back with a replay from seq 1", async () => {
    for (const event of [userTurn, delta, call, SENT, result, done]) emit(event);
    mountBubble();
    await expectOneCard("app-release.apk");
  });

  it("lands when the artifact_ready reaches the pane before its tool_call", async () => {
    emit(userTurn);
    emit(SENT);
    emit(call);
    emit(result);
    mountBubble();
    await expectOneCard("app-release.apk");
  });

  it("is not doubled by a reattach that replays the turn again", async () => {
    for (const event of [userTurn, delta, call, SENT, result, done]) emit(event);
    // The same events again on the same rows (a replay the gate let by)…
    emit(SENT);
    emit(result);
    // …and a rebind, which clears the rows and replays from seq 1.
    selectSession("sess-other", "idle");
    selectSession(SID, "idle");
    for (const event of [userTurn, delta, call, SENT, result, done]) emit(event);
    expect(chat.messages.filter((m) => m.role === "assistant")).toHaveLength(1);
    mountBubble();
    await expectOneCard("app-release.apk");
  });
});
