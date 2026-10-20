// @vitest-environment jsdom
//
// TD-5117: a delegated worker's transcript never reaches the parent model,
// so the window is the one place to read it. The daemon sends the steps on
// the delegate call's tool_result; the chat fold and the activity row both
// show them. Fed from the daemon's own fixture.

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

import { chat, initChat, teardownChat } from "./chat-store.svelte.js";
import MessageBubble from "./components/chat/MessageBubble.svelte";
import TimelineEntryRow from "./components/TimelineEntryRow.svelte";
import type { DaemonEventUnion, ToolResult, WorkerStep } from "./protocol";
import fixtures from "./protocol-fixtures.json";
import { resetDisclosures } from "./reasoning-disclosure.svelte.js";
import { eventToEntry } from "./timeline";
import { formatWorkerTranscript } from "./worker-transcript";

const delegateResult = fixtures.tool_result_delegate as ToolResult;
const SID = delegateResult.session_id;
const MARKER = "zebra-quokka-7731";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
  for (const handler of [...socket.handlers]) handler(event);
  flushSync();
}

function bind(): void {
  emit({
    type: "session_list",
    seq: 1,
    sessions: [
      {
        session_id: SID,
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-10-20T10:00:00Z",
        updated_at: "2026-10-20T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  } as DaemonEventUnion);
}

const delegateCall = {
  type: "tool_call",
  seq: 8,
  session_id: SID,
  tool_call_id: delegateResult.tool_call_id,
  name: "delegate",
  arguments: { task: "Which pair is in animals.md?", kind: "research" },
  decision_class: "B",
} as DaemonEventUnion;

beforeEach(() => {
  initChat();
  bind();
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

describe("formatWorkerTranscript", () => {
  it("lays the steps out in order with the tool and outcome", () => {
    const steps: WorkerStep[] = [
      { kind: "task", text: "Find the pair" },
      { kind: "tool_call", text: '{"path": "a.md"}', tool: "fs_read" },
      { kind: "tool_result", text: "nope", tool: "fs_read", status: "error", truncated: true },
      { kind: "reply", text: "No pair." },
      { kind: "note", text: "Worker stopped: iteration cap." },
    ];
    expect(formatWorkerTranscript(steps)).toBe(
      [
        "Task:\nFind the pair",
        '→ fs_read {"path": "a.md"}',
        "← fs_read (error)\nnope\n… (cut)",
        "Worker:\nNo pair.",
        "· Worker stopped: iteration cap.",
      ].join("\n\n"),
    );
  });
});

describe("the delegate transcript in the chat", () => {
  it("lands on the tool block and shows in the open fold", () => {
    emit({ type: "user_turn", seq: 7, session_id: SID, turn_id: "t1", content: "Research" } as DaemonEventUnion);
    emit(delegateCall);
    emit(delegateResult as DaemonEventUnion);

    const row = chat.messages.find((m) => m.role === "assistant");
    expect(row?.tools?.[0].transcript).toHaveLength(4);
    expect(row?.tools?.[0].output).not.toContain(MARKER);

    if (row === undefined) throw new Error("no assistant row");
    app = mount(MessageBubble, { target: document.body, props: { message: row } });
    flushSync();
    const summary = document.querySelector<HTMLButtonElement>(".fold .summary");
    if (summary === null) throw new Error("no tool fold");
    summary.click();
    flushSync();
    const body = document.querySelector(".fold .body")?.textContent ?? "";
    expect(body).toContain("Worker transcript");
    expect(body).toContain(MARKER);
    expect(body).toContain("animals.md names one animal pair.");
  });

  it("leaves other tools' blocks without a transcript", () => {
    emit({ type: "user_turn", seq: 7, session_id: SID, turn_id: "t1", content: "Read" } as DaemonEventUnion);
    emit({ ...delegateCall, name: "fs_read", arguments: { path: "a.md" } } as DaemonEventUnion);
    emit({ ...delegateResult, transcript: [] } as DaemonEventUnion);
    const row = chat.messages.find((m) => m.role === "assistant");
    expect(row?.tools?.[0].transcript).toBeUndefined();
  });
});

describe("the delegate transcript in the activity timeline", () => {
  it("is its own section of the expanded row, not a raw field", () => {
    const entry = eventToEntry(delegateResult as DaemonEventUnion);
    if (entry === null) throw new Error("no row");
    app = mount(TimelineEntryRow, {
      target: document.body,
      props: { entry, expanded: true, ontoggle: () => {} },
    });
    flushSync();
    const titles = [...document.querySelectorAll(".section-title")].map((el) => el.textContent);
    expect(titles).toContain("Worker transcript");
    expect(document.body.textContent).toContain(MARKER);
    const keys = [...document.querySelectorAll(".field-key")].map((el) => el.textContent);
    expect(keys).not.toContain("transcript");
  });

  it("adds no transcript detail to a plain tool result", () => {
    const entry = eventToEntry(fixtures.tool_result as DaemonEventUnion);
    expect(entry?.details).not.toHaveProperty("transcript");
  });
});
