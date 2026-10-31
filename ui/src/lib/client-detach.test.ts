// A session the window stopped following (TD-5201).
//
// The daemon answers an attach with the session's whole replay, and it reads
// the socket in order: a detach sent while that replay is still on its way is
// read only after the replay has gone out. Clicking one chat and then another
// before the first one's replay lands is enough. Those frames used to count:
// they rebuilt the session's cursor, so the next attach asked to start after
// them, the daemon had nothing newer, and the cleared pane showed the New
// chat screen for a chat with a whole history.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ProtocolClient, type SocketLike } from "./client";

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.onclose?.();
  }

  push(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  attaches(): Array<{ session_id: string; from_seq: number }> {
    return this.sent.map((raw) => JSON.parse(raw)).filter((m) => m.type === "attach");
  }
}

async function connected() {
  const sockets: FakeSocket[] = [];
  const onEvent = vi.fn();
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
      baseBackoffMs: 5,
      maxBackoffMs: 8,
    },
    { onEvent },
  );
  await client.start();
  sockets[0].onopen?.();
  sockets[0].push({ type: "hello_ack", version: 1 });
  return { client, sockets, onEvent };
}

function replay(socket: FakeSocket, sessionId: string, from: number, to: number): void {
  for (let seq = from; seq <= to; seq++) {
    socket.push({ type: "assistant_delta", session_id: sessionId, delta: `d${seq}`, seq });
  }
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("a replay that lands after its detach (TD-5201)", () => {
  it("leaves no cursor behind, so the next attach replays the whole log", async () => {
    const { client, sockets } = await connected();
    client.attach("old");
    client.detach("old");
    client.attach("other");
    // The daemon sends old's replay before it reads the detach.
    replay(sockets[0], "old", 1, 3);
    expect(client.lastSeq("old")).toBe(0);

    client.detach("other");
    client.attach("old");
    expect(sockets[0].attaches().at(-1)).toEqual({ type: "attach", session_id: "old", from_seq: 1 });
    client.stop();
  });

  it("ignores a late log_trimmed too, which used to move the cursor up to the window", async () => {
    const { client, sockets } = await connected();
    client.attach("long");
    client.detach("long");
    client.attach("other");
    sockets[0].push({
      type: "log_trimmed",
      session_id: "long",
      seq: 1,
      requested_from_seq: 1,
      earliest_seq: 50,
    });
    replay(sockets[0], "long", 50, 52);
    expect(client.lastSeq("long")).toBe(0);

    client.detach("other");
    client.attach("long");
    expect(sockets[0].attaches().at(-1)?.from_seq).toBe(1);
    client.stop();
  });

  it("keeps the frames from the stores that no longer show that session", async () => {
    const { client, sockets, onEvent } = await connected();
    client.attach("old");
    client.detach("old");
    onEvent.mockClear();
    replay(sockets[0], "old", 1, 2);
    expect(onEvent).not.toHaveBeenCalled();
    client.stop();
  });

  it("does not reconnect for the rest of a replay cut off mid-way while another chat is open", async () => {
    const { client, sockets, onEvent } = await connected();
    client.attach("a");
    replay(sockets[0], "a", 1, 3);
    client.detach("a");
    client.attach("b");
    replay(sockets[0], "a", 4, 5);
    sockets[0].push({ type: "session_state", session_id: "b", state: "running", seq: 1 });
    expect(sockets).toHaveLength(1);
    expect(client.lastSeq("b")).toBe(1);
    expect(onEvent.mock.calls.at(-1)?.[0]).toMatchObject({ session_id: "b", seq: 1 });
    client.stop();
  });

  it("still delivers a session it was never told to follow, like a new session's reply", async () => {
    const { client, sockets, onEvent } = await connected();
    sockets[0].push({ type: "session_state", session_id: "fresh", state: "running", seq: 1 });
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(client.lastSeq("fresh")).toBe(1);
    client.stop();
  });

  it("still delivers an error about the session it let go, which is no replay", async () => {
    const { client, sockets, onEvent } = await connected();
    client.attach("old");
    client.detach("old");
    sockets[0].push({ type: "error", code: "session_not_running", message: "no", session_id: "old" });
    expect(onEvent).toHaveBeenCalledTimes(1);
    client.stop();
  });

  it("does not reconnect when the chat is opened again before the rest of its replay lands", async () => {
    // Open a, then b, then a again: the daemon still owes the first replay's
    // tail, and it comes ahead of the new replay, past the cursor the detach
    // reset. Read as a gap, it used to force a reconnect and a second replay.
    const { client, sockets, onEvent } = await connected();
    client.attach("a");
    replay(sockets[0], "a", 1, 3);
    client.detach("a");
    client.attach("b");
    client.detach("b");
    client.attach("a");
    onEvent.mockClear();
    replay(sockets[0], "a", 4, 5);
    replay(sockets[0], "a", 1, 5);
    expect(sockets).toHaveLength(1);
    expect(client.lastSeq("a")).toBe(5);
    expect(onEvent.mock.calls.map((call) => call[0].seq)).toEqual([1, 2, 3, 4, 5]);
    client.stop();
  });

  it("takes a trimmed log's notice as where the new replay begins", async () => {
    const { client, sockets } = await connected();
    client.attach("long");
    client.detach("long");
    client.attach("long");
    replay(sockets[0], "long", 9001, 9003);
    sockets[0].push({
      type: "log_trimmed",
      session_id: "long",
      seq: 1,
      requested_from_seq: 1,
      earliest_seq: 9001,
    });
    replay(sockets[0], "long", 9001, 9004);
    expect(sockets).toHaveLength(1);
    expect(client.lastSeq("long")).toBe(9004);
    client.stop();
  });

  it("follows the session again from its first frame once it is attached again", async () => {
    const { client, sockets, onEvent } = await connected();
    client.attach("old");
    client.detach("old");
    client.attach("old");
    onEvent.mockClear();
    replay(sockets[0], "old", 1, 2);
    expect(onEvent).toHaveBeenCalledTimes(2);
    expect(client.lastSeq("old")).toBe(2);
    client.stop();
  });
});
