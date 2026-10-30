// An older finished chat opens with its history (TD-5201).
//
// The real client and the chat store, wired the way the window wires them,
// over a fake daemon that reads one socket in order as the real one does:
// an attach is answered with the whole replay before the next frame is read.
// The owner's report was "clicking on older finished chats goes to the New
// chat screen": a chat clicked, and left for another before its replay
// landed, came back empty on every later click.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ProtocolClient, type SocketLike } from "./client";
import { createChatState, createChatStore, type ChatStore } from "./chat-store";
import type { DaemonEventUnion } from "./protocol";

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  read = 0;

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.onclose?.();
  }
}

interface Log {
  earliest: number;
  events: DaemonEventUnion[];
}

/** Answers what the client sent so far, in order, as one daemon socket does. */
function serve(socket: FakeSocket, logs: Map<string, Log>): void {
  while (socket.read < socket.sent.length) {
    const msg = JSON.parse(socket.sent[socket.read++]);
    if (msg.type !== "attach") continue;
    const log = logs.get(msg.session_id);
    if (log === undefined) continue;
    const frames: unknown[] = [];
    if (msg.from_seq < log.earliest) {
      frames.push({
        type: "log_trimmed",
        seq: 1,
        session_id: msg.session_id,
        requested_from_seq: msg.from_seq,
        earliest_seq: log.earliest,
      });
    }
    frames.push(...log.events.filter((e) => e.seq >= msg.from_seq));
    for (const frame of frames) socket.onmessage?.({ data: JSON.stringify(frame) });
  }
}

function finishedChat(id: string, first: number, ask: string, answer: string): Log {
  const events: DaemonEventUnion[] = [
    { type: "session_state", session_id: id, state: "running", seq: first },
    { type: "user_turn", session_id: id, turn_id: `${id}-t1`, content: ask, seq: first + 1 },
    { type: "assistant_delta", session_id: id, delta: answer, seq: first + 2 },
    {
      type: "turn_complete",
      session_id: id,
      tokens: 1,
      cost: 0,
      tier: "brain",
      duration: 1,
      failed: false,
      error_code: null,
      seq: first + 3,
    },
  ];
  return { earliest: first, events };
}

async function wiredWindow(): Promise<{ store: ChatStore; socket: FakeSocket }> {
  const sockets: FakeSocket[] = [];
  let store: ChatStore | null = null;
  const client = new ProtocolClient(
    {
      async getDaemonInfo() {
        return { port: 9000, token: "t" };
      },
      socketFactory: () => {
        const socket = new FakeSocket();
        sockets.push(socket);
        return socket;
      },
    },
    { onEvent: (event) => store?.applyEvent(event) },
  );
  store = createChatStore(
    {
      send: (msg) => client.send(msg),
      attach: (id) => client.attach(id),
      detach: (id) => client.detach(id),
      drafts: { restore: () => "", schedule: () => {}, flush: () => {} },
    },
    createChatState(),
  );
  await client.start();
  const socket = sockets[0];
  socket.onopen?.();
  socket.onmessage?.({ data: JSON.stringify({ type: "hello_ack", version: 1 }) });
  socket.read = socket.sent.length;
  return { store, socket };
}

function texts(store: ChatStore): string[] {
  return store.state.messages.map((m) => `${m.role}: ${m.text}`);
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("reopening a chat left before its replay landed (TD-5201)", () => {
  it("shows the whole conversation again, not the New chat screen", async () => {
    const { store, socket } = await wiredWindow();
    const logs = new Map([
      ["older", finishedChat("older", 1, "Plan the trip", "Here is the plan.")],
      ["newer", finishedChat("newer", 1, "Hi", "Hello.")],
    ]);
    // Two clicks in a row: the first chat's replay is still on its way.
    store.selectSession("older", "running");
    store.selectSession("newer", "running");
    serve(socket, logs);
    expect(texts(store)).toEqual(["user: Hi", "assistant: Hello."]);

    store.selectSession("older", "running");
    serve(socket, logs);
    expect(texts(store)).toEqual(["user: Plan the trip", "assistant: Here is the plan."]);
  });

  it("shows a long chat's kept window again, past its log_trimmed", async () => {
    const { store, socket } = await wiredWindow();
    const logs = new Map([
      ["long", finishedChat("long", 9001, "Keep going", "Done.")],
      ["newer", finishedChat("newer", 1, "Hi", "Hello.")],
    ]);
    store.selectSession("long", "running");
    store.selectSession("newer", "running");
    serve(socket, logs);

    store.selectSession("long", "running");
    serve(socket, logs);
    expect(texts(store)).toContain("user: Keep going");
    expect(texts(store)).toContain("assistant: Done.");
  });
});
