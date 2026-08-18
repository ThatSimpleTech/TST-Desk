// Tests for the decisions-ledger store (TD-1202).
//
// The store reads the daemon stream through connection-status and the active
// session/workspace through session-status, so the tests mock exactly those
// seams and drive real decision_logged events through the reducer.

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => {
  const state = {
    handler: null as ((e: DaemonEventUnion) => void) | null,
    session: { sessionId: "s1" as string | null, workspacePath: "/home/u/proj" as string | null },
  };
  return state;
});

vi.mock("./connection-status.svelte.js", () => ({
  onEvent: (handler: (e: DaemonEventUnion) => void) => {
    mocks.handler = handler;
    return () => {
      mocks.handler = null;
    };
  },
  sendToDaemon: (_msg: ClientMessageUnion) => true,
}));

vi.mock("./session-status.svelte.js", () => ({
  get session() {
    return mocks.session;
  },
}));

import { ProtocolClient, type SocketLike } from "./client";
import { createChatStore } from "./chat-store";
import {
  decisions,
  startDecisions,
  resetDecisions,
  bindDecisions,
  filteredDecisions,
  setClassFilter,
  openDecisions,
  closeDecisions,
  ledgerPath,
  LEDGER_RELATIVE_PATH,
} from "./decisions.svelte.js";

function emit(event: DaemonEventUnion): void {
  mocks.handler?.(event);
}

const logged = (
  seq: number,
  decisionClass: "A" | "B" | "C",
  commit: string | null,
  sessionId = "s1",
): DaemonEventUnion =>
  ({
    type: "decision_logged",
    session_id: sessionId,
    seq,
    decision_class: decisionClass,
    what: `chose thing ${seq}`,
    why: `because ${seq}`,
    commit,
  }) as DaemonEventUnion;

beforeEach(() => {
  resetDecisions();
  mocks.session.sessionId = "s1";
  mocks.session.workspacePath = "/home/u/proj";
});

describe("stream reduce", () => {
  it("collects decision_logged events for the active session", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    expect(decisions.rows).toHaveLength(2);
    expect(decisions.rows[0].what).toContain("chose thing 1");
  });

  it("ignores other sessions' decisions", () => {
    startDecisions();
    emit(logged(1, "A", "abc123", "other-session"));
    expect(decisions.rows).toHaveLength(0);
  });

  it("ignores everything until a session is active", () => {
    mocks.session.sessionId = null;
    startDecisions();
    emit(logged(1, "A", "abc123"));
    expect(decisions.rows).toHaveLength(0);
  });

  it("drops a replayed seq so a re-attach cannot double a row (TD-1203)", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    expect(decisions.rows.map((r) => r.seq)).toEqual([1, 2]);
  });
});

describe("session bind (TD-1203)", () => {
  it("binding another session drops what the previous one left", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    bindDecisions("s2");
    expect(decisions.rows).toHaveLength(0);
    emit(logged(1, "A", "s2commit", "s2"));
    expect(decisions.rows).toHaveLength(1);
    expect(decisions.rows[0].id).toBe("s2:1");
  });

  it("re-binding the session already shown keeps the rows", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    bindDecisions("s1");
    expect(decisions.rows).toHaveLength(1);
  });

  it("binding null unbinds and empties", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    bindDecisions(null);
    emit(logged(2, "A", "def456"));
    expect(decisions.rows).toHaveLength(0);
  });

  it("re-binding resets the log position so a fresh replay is folded whole", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    bindDecisions("s2");
    bindDecisions("s1");
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    expect(decisions.rows.map((r) => r.id)).toEqual(["s1:1", "s1:2"]);
  });
});

describe("revert command (AC: copyable revert per entry)", () => {
  it("derives git revert from the attributed commit", () => {
    startDecisions();
    emit(logged(1, "A", "deadbeef"));
    expect(decisions.rows[0].undoCommand).toBe("git revert deadbeef");
  });

  it("offers none for a Class B entry without a commit", () => {
    startDecisions();
    emit(logged(1, "B", null));
    expect(decisions.rows[0].undoCommand).toBeNull();
  });
});

describe("class filter (AC)", () => {
  it("narrows rows to the chosen class — the under-a-minute Class A scan", () => {
    startDecisions();
    emit(logged(1, "A", "abc123"));
    emit(logged(2, "B", null));
    emit(logged(3, "A", "def456"));
    setClassFilter("A");
    const rows = filteredDecisions();
    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.decisionClass === "A")).toBe(true);
    setClassFilter(null);
    expect(filteredDecisions()).toHaveLength(3);
  });
});

describe("panel open/close and ledger path", () => {
  it("opens, closes", () => {
    openDecisions();
    expect(decisions.open).toBe(true);
    closeDecisions();
    expect(decisions.open).toBe(false);
  });

  it("names the workspace ledger file (the link-out target)", () => {
    expect(ledgerPath()).toBe(`/home/u/proj/${LEDGER_RELATIVE_PATH}`);
    expect(LEDGER_RELATIVE_PATH).toBe(".tst/autonomy/DECISIONS.md");
  });

  it("no workspace, no path", () => {
    mocks.session.workspacePath = null;
    expect(ledgerPath()).toBeNull();
  });
});

// ── A → B → A through a real client (TD-1203 AC 3) ─────────────────────
//
// Same harness shape as timeline-scope.test.ts: a real ProtocolClient
// talking to a fake daemon that answers attach with a real replay. The
// decisions store is fed the way AppShell feeds it — onEvent — and bind
// rides the chat store's onBind, the same moment the pane attaches.

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

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

describe("A → B → A through a real client (TD-1203)", () => {
  it("switching away and back shows each decision once, not twice", async () => {
    const daemon = new FakeDaemon();
    startDecisions();

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
          store.applyEvent(event);
          emit(event);
        },
      },
    );

    const store = createChatStore({
      send: (msg) => client.send(msg),
      attach: (id) => client.attach(id),
      detach: (id) => client.detach(id),
      onBind: bindDecisions,
    });

    await client.start();
    await flush();

    store.selectSession("session-a", "idle");
    daemon.emit("session-a", {
      type: "decision_logged",
      decision_class: "A",
      what: "wrote a",
      why: "because a",
      commit: "aaa",
    });
    expect(decisions.rows.map((r) => r.id)).toEqual(["session-a:1"]);

    store.selectSession("session-b", "idle");
    daemon.emit("session-b", {
      type: "decision_logged",
      decision_class: "B",
      what: "wrote b",
      why: "because b",
      commit: null,
    });
    expect(decisions.rows.map((r) => r.id)).toEqual(["session-b:1"]);

    store.selectSession("session-a", "idle");
    expect(decisions.rows.map((r) => r.id)).toEqual(["session-a:1"]);
    expect(decisions.rows.map((r) => r.what)).toEqual(["wrote a"]);
  });
});
