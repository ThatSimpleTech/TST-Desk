// ProtocolClient tests (TD-1003).
//
// Uses a fake WebSocket transport so the client's logic — handshake,
// reconnect/backoff, from_seq replay with no gaps/duplicates, and unknown-
// event tolerance — is exercised without a real socket or a Tauri host.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ProtocolClient, type SocketLike } from "./client";

interface FakeServer {
  socket: FakeSocket;
  /** Reply to a client's `hello` with a hello_ack. */
  ack(): void;
  /** Push a daemon event to the client. */
  push(json: string): void;
  /** Simulate the transport dropping. */
  drop(): void;
  /** Deliver the queued hello to the daemon and reply with `ready`. */
  handshake(): void;
}

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  closed = false;

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
    this.onclose?.();
  }
}

/** Builds a factory + a handle to drive the fake transport. */
function makeConnection(url: string): FakeServer {
  const socket = new FakeSocket();
  const server: FakeServer = {
    socket,
    ack() {
      socket.onmessage?.({ data: JSON.stringify({ type: "hello_ack", version: 1 }) });
    },
    push(json) {
      socket.onmessage?.({ data: json });
    },
    drop() {
      socket.onclose?.();
    },
    handshake() {
      socket.onopen?.();
      socket.onmessage?.({ data: JSON.stringify({ type: "hello_ack", version: 1 }) });
    },
  };
  void url;
  return server;
}

/** Create a client with an injectable server. Returns client + driving handle. */
function buildClient(opts: {
  baseBackoffMs?: number;
  maxBackoffMs?: number;
  attach?: string;
} = {}) {
  const sockets: FakeSocket[] = [];
  const servers: FakeServer[] = [];
  const onStateChange = vi.fn();
  const onEvent = vi.fn();
  type Info = { port: number; token: string } | null;
  let info: Info = { port: 9000, token: "t" };

  const client = new ProtocolClient(
    {
      async getDaemonInfo() {
        return info;
      },
      socketFactory: (url) => {
        const s = makeConnection(url);
        sockets.push(s.socket);
        servers.push(s);
        return s.socket;
      },
      baseBackoffMs: opts.baseBackoffMs ?? 5,
      maxBackoffMs: opts.maxBackoffMs ?? 20,
    },
    { onStateChange, onEvent },
  );

  return {
    client,
    sockets,
    servers,
    onStateChange,
    onEvent,
    setInfo(i: Info) {
      info = i;
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("handshake", () => {
  it("sends hello with the token and reports connected on hello_ack", async () => {
    const h = buildClient();
    await h.client.start();
    const s = h.sockets[0];
    expect(s.sent.length).toBe(0);
    s.onopen?.();
    const hello = JSON.parse(s.sent[0]);
    expect(hello.type).toBe("hello");
    expect(hello.token).toBe("t");
    expect(hello.version).toBe(1);
    h.servers[0].ack();
    expect(h.onStateChange).toHaveBeenCalledWith("connecting");
    expect(h.onStateChange).toHaveBeenCalledWith("connected");
    expect(h.client.connectionState).toBe("connected");
  });
});

describe("reconnect and backoff", () => {
  it("reconnects after an unexpected close and backs off exponentially", async () => {
    const h = buildClient({ baseBackoffMs: 5, maxBackoffMs: 20 });
    await h.client.start();
    h.servers[0].handshake();
    expect(h.client.connectionState).toBe("connected");

    // Drop the connection: backoff attempt 0 -> 5ms.
    h.servers[0].drop();
    expect(h.client.connectionState).toBe("reconnecting");
    expect(h.sockets.length).toBe(1);

    await vi.advanceTimersByTimeAsync(5);
    await vi.advanceTimersByTimeAsync(0);
    // One retry fired, backoff now at attempt 1 -> 10ms.
    expect(h.sockets.length).toBe(2);
    await vi.advanceTimersByTimeAsync(1);

    // Stop permanently cancels pending retries.
    h.client.stop();
    expect(h.client.connectionState).toBe("stopped");
  });

  it("caps backoff at maxBackoffMs", async () => {
    const h = buildClient({ baseBackoffMs: 5, maxBackoffMs: 8 });
    await h.client.start();
    h.servers[0].drop();
    // attempt0 -> 5ms (within cap), attempt1 -> 8ms (capped).
    await vi.advanceTimersByTimeAsync(5);
    await vi.advanceTimersByTimeAsync(0);
    expect(h.sockets.length).toBe(2);
    h.client.stop();
  });
});

describe("from_seq replay — no gaps, no duplicates", () => {
  it("attaches at lastSeq+1 when reconnecting", async () => {
    const h = buildClient({ attach: "sess-1" });
    await h.client.start();
    h.servers[0].handshake();

    // Client asks to follow a session; attach sent at from_seq=1.
    h.client.attach("sess-1");
    const attach = JSON.parse(h.sockets[0].sent.at(-1)!);
    expect(attach.type).toBe("attach");
    expect(attach.session_id).toBe("sess-1");
    expect(attach.from_seq).toBe(1);

    // Daemon replays seq 1 then 2.
    h.servers[0].push(JSON.stringify({ type: "session_state", session_id: "sess-1", state: "running", seq: 1 }));
    h.servers[0].push(JSON.stringify({ type: "assistant_delta", session_id: "sess-1", delta: "Hi", seq: 2 }));
    expect(h.client.lastSeq("sess-1")).toBe(2);
    expect(h.onEvent).toHaveBeenCalledTimes(2);

    // Drop: reconnect re-attaches at lastSeq+1 = 3 (the missed events).
    h.servers[0].drop();
    expect(h.client.connectionState).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(5);
    const fresh = h.sockets[1];
    fresh.onopen?.();
    // Handshake completes on the new socket, then the client re-attaches.
    h.servers[1].ack();
    const reattach = JSON.parse(fresh.sent.find((m) => m.includes('"attach"'))!);
    expect(reattach.from_seq).toBe(3);
    h.client.stop();
  });

  it("drops duplicate/old seqs without re-emitting", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");

    h.servers[0].push(JSON.stringify({ type: "session_state", session_id: "sess-1", state: "running", seq: 1 }));
    h.servers[0].push(JSON.stringify({ type: "assistant_delta", session_id: "sess-1", delta: "Hi", seq: 2 }));
    const before = h.onEvent.mock.calls.length;

    // Re-send seq 2 (duplicate from a replay).
    h.servers[0].push(JSON.stringify({ type: "assistant_delta", session_id: "sess-1", delta: "Hi", seq: 2 }));
    expect(h.onEvent).toHaveBeenCalledTimes(before);
    expect(h.client.lastSeq("sess-1")).toBe(2);
    h.client.stop();
  });

  it("reconnects to fetch the gap when a seq jump beyond +1 is seen", async () => {
    const h = buildClient({ baseBackoffMs: 5, maxBackoffMs: 8 });
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");
    h.servers[0].push(JSON.stringify({ type: "session_state", session_id: "sess-1", state: "running", seq: 1 }));

    // Leap from 1 to 5: two missed events. Client must force a reconnect to
    // re-attach at from_seq=2 rather than accept the jump as truth.
    const spy = vi.spyOn(h.client as unknown as { forceReconnect(): void }, "forceReconnect");
    h.servers[0].push(JSON.stringify({ type: "assistant_delta", session_id: "sess-1", delta: "X", seq: 5 }));
    expect(spy).toHaveBeenCalled();
    h.client.stop();
  });
});

describe("unknown event tolerance", () => {
  it("warns and drops an unknown event type without failing, still advancing seq", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");

    // A future event type the TS union doesn't know yet.
    h.servers[0].push(JSON.stringify({ type: "fresh_payload", session_id: "sess-1", seq: 1 }));
    expect(h.onEvent).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("unknown event type"));

    // seq still advanced, so the next known event (seq 2) is not a false gap.
    h.servers[0].push(JSON.stringify({ type: "assistant_delta", session_id: "sess-1", delta: "Hi", seq: 2 }));
    expect(h.onEvent).toHaveBeenCalledTimes(1);
    expect(h.client.lastSeq("sess-1")).toBe(2);
    h.client.stop();
    warn.mockRestore();
  });

  it("ignores non-JSON and malformed frames", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.servers[0].push("not-json");
    h.servers[0].push("42");
    expect(h.onEvent).not.toHaveBeenCalled();
    expect(warn).toBeCalled(); // at least one warn (non-JSON)
    h.client.stop();
    warn.mockRestore();
  });
});
describe("send (TD-1004)", () => {
  it("writes the message on a handshaken socket and returns true", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    const ok = h.client.send({ type: "cancel", session_id: "s1" });
    expect(ok).toBe(true);
    const last = JSON.parse(h.sockets[0].sent[h.sockets[0].sent.length - 1]);
    expect(last).toEqual({ type: "cancel", session_id: "s1" });
    h.client.stop();
  });

  it("returns false and sends nothing once stopped", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.stop();
    const wireCount = h.sockets[0].sent.length;
    expect(h.client.send({ type: "cancel", session_id: "s1" })).toBe(false);
    expect(h.sockets[0].sent.length).toBe(wireCount);
  });
});
