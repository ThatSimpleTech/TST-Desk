// Activity-timeline session-scope regression tests (TD-1009).
//
// The defect: every daemon event the connection carried was appended to one
// process-wide list, so attaching to a second session showed the first
// session's activity underneath it — and the Files pane (TD-1705), which
// folds the same store, inherited the symptom.
//
// The harness is the real machinery, not a mock of it: a real ProtocolClient
// talking to a fake daemon that keeps a per-session event log and answers
// `attach` with a real replay, the real chat store deciding which session the
// pane is bound to, and the real timeline store fed exactly the way AppShell
// feeds it. Anything less would assert the fix back to itself.

import { describe, it, expect, beforeEach } from "vitest";
import { ProtocolClient, type SocketLike } from "./client";
import { createChatStore } from "./chat-store";
import { Timeline } from "./timeline";
import { entries, push, bindSession } from "./timeline-store.svelte.js";
import { foldFileWrites } from "./files";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const A = "session-a";
const B = "session-b";

// ── The scope rules themselves, on the pure store ───────────────────────

/** A tool_call on `sessionId` at `seq` — the smallest event that makes an
 *  entry, so the assertions are about scope and nothing else. */
function call(sessionId: string, seq: number, name = "fs_read"): DaemonEventUnion {
  return {
    type: "tool_call",
    session_id: sessionId,
    tool_call_id: `tc-${seq}`,
    name,
    arguments: { path: "x" },
    seq,
  } as DaemonEventUnion;
}

describe("Timeline session scope (TD-1009)", () => {
  it("an unbound timeline folds nothing — no session, no activity", () => {
    const t = new Timeline();
    t.push(call(A, 1));
    expect(t.sessionId).toBeNull();
    expect(t.length).toBe(0);
  });

  it("keeps only the bound session's events", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 1, "mine"));
    t.push(call(B, 1, "theirs"));
    expect(t.entries.map((e) => e.title)).toEqual(["mine"]);
  });

  it("binding another session drops what the previous one left", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 1, "mine"));
    t.bind(B);
    expect(t.length).toBe(0);
    t.push(call(B, 1, "theirs"));
    expect(t.entries.map((e) => e.title)).toEqual(["theirs"]);
  });

  it("re-binding the session already shown keeps the entries", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 1, "mine"));
    t.bind(A);
    expect(t.entries.map((e) => e.title)).toEqual(["mine"]);
  });

  it("binding null unbinds and empties", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 1));
    t.bind(null);
    expect(t.sessionId).toBeNull();
    expect(t.length).toBe(0);
  });

  it("drops a replayed event already folded in, so a re-attach cannot double it", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 1));
    t.push(call(A, 2));
    // What an attach at from_seq 1 replays: the log from the top.
    t.push(call(A, 1));
    t.push(call(A, 2));
    expect(t.entries.map((e) => e.seq)).toEqual([1, 2]);
  });

  it("a replayed shell_output chunk is not appended to the buffer twice", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 1, "shell"));
    const chunk = {
      type: "shell_output",
      session_id: A,
      tool_call_id: "tc-1",
      stream: "stdout",
      chunk: "a.txt\n",
      seq: 2,
    } as DaemonEventUnion;
    t.push(chunk);
    t.push(chunk);
    expect(t.entries[0].stdout).toBe("a.txt\n");
  });

  it("re-binding resets the log position, so the fresh replay is folded whole", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 1));
    t.push(call(A, 2));
    t.bind(B);
    t.bind(A);
    // Switching away and back makes the client ask from_seq 1 again.
    t.push(call(A, 1));
    t.push(call(A, 2));
    expect(t.entries.map((e) => e.seq)).toEqual([1, 2]);
  });

  it("drops an error that names no session — it is no session's activity", () => {
    const t = new Timeline();
    t.bind(A);
    t.push({ type: "error", code: "protocol_error", message: "bad frame" } as DaemonEventUnion);
    expect(t.length).toBe(0);
  });

  it("keeps a session's error even though it carries no seq to dedupe on", () => {
    const t = new Timeline();
    t.bind(A);
    t.push(call(A, 5));
    // build_error writes straight to the socket, outside the event log.
    t.push({
      type: "error",
      session_id: A,
      code: "session_not_running",
      message: "that session has ended",
    } as DaemonEventUnion);
    expect(t.entries.map((e) => e.kind)).toEqual(["tool_call", "error"]);
  });
});

// ── The same rules through the real client, chat store, and fan-out ─────

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(private readonly onSend: (raw: string) => void) {}

  send(raw: string): void {
    this.onSend(raw);
  }

  close(): void {
    this.onclose?.();
  }
}

/** A daemon with a per-session event log and a real attach replay. */
class FakeDaemon {
  socket: FakeSocket | null = null;
  private readonly logs = new Map<string, Record<string, unknown>[]>();

  connect(socket: FakeSocket): void {
    this.socket = socket;
  }

  private log(sessionId: string): Record<string, unknown>[] {
    const existing = this.logs.get(sessionId);
    if (existing !== undefined) return existing;
    const fresh: Record<string, unknown>[] = [];
    this.logs.set(sessionId, fresh);
    return fresh;
  }

  private deliver(frame: Record<string, unknown>): void {
    this.socket?.onmessage?.({ data: JSON.stringify(frame) });
  }

  /** Append to the session's log (assigning the next seq) and stream it. */
  emit(sessionId: string, event: Record<string, unknown>): void {
    const log = this.log(sessionId);
    const frame = { ...event, session_id: sessionId, seq: log.length + 1 };
    log.push(frame);
    this.deliver(frame);
  }

  receive(raw: string): void {
    const msg = JSON.parse(raw) as ClientMessageUnion;
    if (msg.type === "hello") {
      this.deliver({ type: "hello_ack", version: 1 });
      return;
    }
    if (msg.type === "attach") {
      for (const frame of this.log(msg.session_id)) {
        if ((frame.seq as number) >= msg.from_seq) this.deliver(frame);
      }
    }
  }
}

interface Harness {
  daemon: FakeDaemon;
  client: ProtocolClient;
  store: ReturnType<typeof createChatStore>;
}

/** Flush the microtask queue — `start()` awaits the daemon-info lookup. */
async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

/** A connected client wired the way the app wires it: the chat store binds
 *  the pane and the timeline store rides the same event fan-out. */
async function boot(): Promise<Harness> {
  const daemon = new FakeDaemon();

  const client = new ProtocolClient(
    {
      async getDaemonInfo() {
        return { port: 9000, token: "t" };
      },
      socketFactory() {
        const socket = new FakeSocket((raw) => daemon.receive(raw));
        daemon.connect(socket);
        queueMicrotask(() => socket.onopen?.());
        return socket;
      },
    },
    {
      onEvent(event) {
        // ChatPane mounts inside AppShell, so its subscription runs first;
        // the shell's `onEvent(push)` is second. Same order here.
        store.applyEvent(event);
        push(event);
      },
    },
  );

  const store = createChatStore({
    send: (msg) => client.send(msg),
    attach: (id) => client.attach(id),
    detach: (id) => client.detach(id),
    onBind: bindSession,
  });

  await client.start();
  await flush();
  return { daemon, client, store };
}

/** One session's worth of activity: a tool call, its result, and the write
 *  diff the Files pane folds. */
function drive(h: Harness, sessionId: string, label: string): void {
  h.daemon.emit(sessionId, {
    type: "tool_call",
    tool_call_id: `${label}-tc`,
    name: `${label}_tool`,
    arguments: { note: label },
    decision_class: "A",
  });
  h.daemon.emit(sessionId, {
    type: "tool_result",
    tool_call_id: `${label}-tc`,
    status: "success",
    output: `${label} finished`,
    truncated: false,
    diff: `--- a/${label}.txt\n+++ b/${label}.txt\n+${label}\n`,
  });
}

/** Bind the pane to a session the way a rail click does (TD-1701). */
function bindPane(h: Harness, sessionId: string): void {
  h.daemon.emit(sessionId, { type: "session_state", state: "idle" });
  h.store.selectSession(sessionId, "idle");
}

function titles(): string[] {
  return entries.map((e) => e.title);
}

describe("the activity timeline is scoped to the bound session (TD-1009)", () => {
  beforeEach(() => {
    bindSession(null);
  });

  it("shows the second session's activity, not the first's", async () => {
    const h = await boot();

    bindPane(h, A);
    drive(h, A, "alpha");
    expect(titles()).toContain("alpha_tool");

    bindPane(h, B);
    drive(h, B, "bravo");

    // The story's test: two sessions through one client, and the second's
    // view carries none of the first's entries.
    expect(titles()).toContain("bravo_tool");
    for (const title of titles()) expect(title).not.toContain("alpha");
    expect(entries.every((e) => !JSON.stringify(e.details).includes("alpha"))).toBe(true);
  });

  it("the Files pane folds the same scoped entries (TD-1705)", async () => {
    const h = await boot();

    bindPane(h, A);
    drive(h, A, "alpha");
    expect(foldFileWrites(entries).files.map((f) => f.path)).toEqual(["alpha.txt"]);

    bindPane(h, B);
    drive(h, B, "bravo");

    const summary = foldFileWrites(entries);
    expect(summary.files.map((f) => f.path)).toEqual(["bravo.txt"]);
    expect(summary.fileCount).toBe(1);
    expect(summary.writeCount).toBe(1);
  });

  it("a session followed by another lane never lands in the bound pane", async () => {
    const h = await boot();

    bindPane(h, A);
    bindPane(h, B);
    // The title bar's store follows the session it adopted at open (TD-1006)
    // by attaching it on this same connection, so A's events keep arriving
    // after the pane has moved on. They are not this pane's activity.
    h.client.attach(A);
    drive(h, A, "alpha");

    expect(titles()).toHaveLength(0);
  });

  it("re-binding the session already shown does not empty the pane", async () => {
    const h = await boot();

    bindPane(h, A);
    drive(h, A, "alpha");
    const before = entries.length;

    // The rail no-ops a click on the attached row, but the store must not
    // depend on its caller for that: a bind with nothing to re-hydrate from
    // would leave the pane blank for good.
    h.store.selectSession(A, "idle");

    expect(entries).toHaveLength(before);
  });
});

describe("replay after re-attach does not double-count (TD-1009 / TD-1716)", () => {
  beforeEach(() => {
    bindSession(null);
  });

  it("switching away and back replays the whole log without duplicating it", async () => {
    const h = await boot();

    bindPane(h, A);
    drive(h, A, "alpha");
    const alphaTitles = titles();
    expect(alphaTitles).toHaveLength(2);

    bindPane(h, B);
    drive(h, B, "bravo");

    // Back to A: the switch away dropped the client's seq for A, so this
    // attach asks from_seq 1 and the daemon replays A's whole log.
    h.store.selectSession(A, "idle");

    expect(titles()).toEqual(alphaTitles);
  });

  it("a resume re-attach replays the gap without re-folding what is shown", async () => {
    const h = await boot();

    bindPane(h, A);
    drive(h, A, "alpha");
    const before = entries.length;

    // TD-1716: every followed session is re-attached at lastSeq+1 and the
    // daemon replays from there. Nothing already on screen may come back.
    h.client.resume();
    h.client.resume();

    expect(entries).toHaveLength(before);
  });
});
