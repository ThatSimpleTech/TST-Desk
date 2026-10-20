// @vitest-environment jsdom
//
// TD-5125: "Restore files to here", mounted for real. A user row offers it;
// the dialog shows the files and the diff before anything is sent; a file
// changed by hand stays unticked; Edit and resend can take the same path and
// forks only once the files are back. The phone shows these same components.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const connection = vi.hoisted(() => {
  const handlers = new Set<(event: unknown) => void>();
  return { handlers, sent: [] as Record<string, unknown>[], up: true };
});
const chatStore = vi.hoisted(() => ({
  chat: { sessionId: "sess-1" as string | null },
  forks: [] as [number, string][],
}));

vi.mock("./connection-status.svelte.js", () => ({
  onDaemonEvent: (handler: (event: unknown) => void) => {
    connection.handlers.add(handler);
    return () => connection.handlers.delete(handler);
  },
  sendToDaemon: (msg: Record<string, unknown>) => {
    if (!connection.up) return false;
    connection.sent.push(msg);
    return true;
  },
}));

vi.mock("./chat-store.svelte.js", () => ({
  chat: chatStore.chat,
  forkFrom: (userIndex: number, text: string) => {
    chatStore.forks.push([userIndex, text]);
    return true;
  },
  requestCompact: () => true,
  setBranch: () => true,
}));

import MessageBubble from "./components/chat/MessageBubble.svelte";
import RestoreFilesDialog from "./components/RestoreFilesDialog.svelte";
import fixtures from "./protocol-fixtures.json";
import type { ChatMessage } from "./chat-store";
import type { FilesRestored, FilesRestorePreview } from "./protocol";
import { closeRestore, startRewind } from "./rewind-store.svelte.js";

const PREVIEW = fixtures.files_restore_preview as FilesRestorePreview;
const RESTORED = fixtures.files_restored as FilesRestored;

const ROW: ChatMessage = {
  id: "m1",
  role: "user",
  text: "make the button blue",
  complete: true,
  at: 0,
  userIndex: 0,
  siblingIndex: 0,
  siblingCount: 1,
  turnId: "turn-1",
};

let target: HTMLElement;
let mounted: ReturnType<typeof mount>[] = [];
let stop: () => void = () => {};

function emit(event: unknown): void {
  for (const handler of connection.handlers) handler(event);
  flushSync();
}

function button(label: string): HTMLButtonElement {
  const found = [...target.querySelectorAll("button")].find((b) =>
    (b.textContent ?? "").replace(/\s+/g, " ").trim().startsWith(label),
  );
  expect(found, label).toBeDefined();
  return found!;
}

function click(label: string): void {
  button(label).click();
  flushSync();
}

function sentOf(type: string): Record<string, unknown>[] {
  return connection.sent.filter((m) => m.type === type);
}

beforeEach(() => {
  connection.sent.length = 0;
  connection.up = true;
  chatStore.forks.length = 0;
  target = document.createElement("div");
  document.body.appendChild(target);
  stop = startRewind();
  mounted = [
    mount(MessageBubble, { target, props: { message: ROW } }),
    mount(RestoreFilesDialog, { target }),
  ];
  flushSync();
});

afterEach(() => {
  closeRestore();
  for (const m of mounted) unmount(m);
  mounted = [];
  stop();
  target.remove();
});

describe("Restore files to here", () => {
  it("shows the files and the diff before anything is restored", () => {
    click("Restore files to here");
    expect(sentOf("preview_restore_files")).toEqual([
      { type: "preview_restore_files", session_id: "sess-1", turn_id: "turn-1" },
    ]);
    expect(target.textContent).toContain("Looking at what the agent changed");

    emit(PREVIEW);
    const text = target.textContent ?? "";
    expect(text).toContain("src/app.py");
    expect(text).toContain("Delete (the agent made it)");
    expect(text).toContain("Bring back (the agent deleted it)");
    expect(text).toContain("Changes to undo");
    expect(text).toContain("+new");
    expect(sentOf("restore_files")).toEqual([]);
  });

  it("leaves a hand-edited file alone unless it is ticked", () => {
    click("Restore files to here");
    emit(PREVIEW);
    const box = target.querySelector<HTMLInputElement>(".file--hand input[type=checkbox]");
    expect(box?.checked).toBe(false);
    expect(target.textContent).toContain("changed by hand");

    click("Restore 3 files");
    expect(sentOf("restore_files")).toEqual([
      {
        type: "restore_files",
        session_id: "sess-1",
        turn_id: "turn-1",
        plan_id: PREVIEW.plan_id,
        include_hand_edited: [],
      },
    ]);
    emit(RESTORED);
    expect(target.textContent).toContain("Put back 2 files");
  });

  it("restores a hand-edited file the person ticks", () => {
    click("Restore files to here");
    emit(PREVIEW);
    const box = target.querySelector<HTMLInputElement>(".file--hand input[type=checkbox]")!;
    box.click();
    flushSync();
    click("Restore 4 files");
    expect(sentOf("restore_files")[0]?.include_hand_edited).toEqual(["notes.md"]);
  });

  it("Edit and resend offers the restore and forks after it", () => {
    click("Edit");
    const area = target.querySelector<HTMLTextAreaElement>("textarea.edit")!;
    area.value = "make the button green";
    area.dispatchEvent(new Event("input", { bubbles: true }));
    const box = target.querySelector<HTMLInputElement>(".restore-too input")!;
    box.click();
    flushSync();
    click("Save");

    expect(sentOf("preview_restore_files")).toHaveLength(1);
    expect(chatStore.forks).toEqual([]);
    emit(PREVIEW);
    click("Restore 3 files and resend");
    expect(chatStore.forks).toEqual([]);
    emit(RESTORED);
    expect(chatStore.forks).toEqual([[0, "make the button green"]]);
    expect(target.textContent).toContain("Your edited message was resent");
  });

  it("Edit and resend can skip the restore", () => {
    click("Edit");
    const area = target.querySelector<HTMLTextAreaElement>("textarea.edit")!;
    area.value = "make the button green";
    area.dispatchEvent(new Event("input", { bubbles: true }));
    target.querySelector<HTMLInputElement>(".restore-too input")!.click();
    flushSync();
    click("Save");
    emit(PREVIEW);
    click("Resend without restoring");
    expect(chatStore.forks).toEqual([[0, "make the button green"]]);
    expect(sentOf("restore_files")).toEqual([]);
  });

  it("shows a refusal in the dialog", () => {
    click("Restore files to here");
    emit({
      type: "error",
      seq: 1,
      session_id: "sess-1",
      code: "rewind_unavailable",
      message: "This workspace is not a git repository, so there are no checkpoints to restore.",
      in_reply_to: "preview_restore_files",
    });
    expect(target.textContent).toContain("not a git repository");
  });
});
