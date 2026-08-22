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
  it("opens a named host instead of loopback when daemon info supplies one (TD-3701)", async () => {
    const urls: string[] = [];
    const client = new ProtocolClient({
      async getDaemonInfo() {
        return { port: 9000, token: "remote-tok", host: "100.64.1.2" };
      },
      socketFactory: (url) => {
        urls.push(url);
        return new FakeSocket();
      },
    });
    await client.start();
    expect(urls[0]).toBe("ws://100.64.1.2:9000");
    client.stop();
  });

  it("brackets an IPv6 host in the default URL", async () => {
    const urls: string[] = [];
    const client = new ProtocolClient({
      async getDaemonInfo() {
        return { port: 9000, token: "remote-tok", host: "fd7a:115c:a1e0::1" };
      },
      socketFactory: (url) => {
        urls.push(url);
        return new FakeSocket();
      },
    });
    await client.start();
    expect(urls[0]).toBe("ws://[fd7a:115c:a1e0::1]:9000");
    client.stop();
  });

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

describe("send", () => {
  it("sends a client message on a handshaken socket and reports success", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    expect(h.client.connectionState).toBe("connected");

    const ok = h.client.send({ type: "approve", session_id: "sess-1", tool_call_id: "tc-1" });
    expect(ok).toBe(true);
    const sent = JSON.parse(h.sockets[0].sent.at(-1)!);
    expect(sent).toEqual({ type: "approve", session_id: "sess-1", tool_call_id: "tc-1" });
  });

  it("returns false and sends nothing when not connected", async () => {
    const h = buildClient();
    await h.client.start();
    // The socket exists but hello_ack never arrived — not handshaken.
    h.sockets[0].onopen?.();
    const ok = h.client.send({
      type: "deny",
      session_id: "sess-1",
      tool_call_id: "tc-1",
      reason: "not safe",
    });
    expect(ok).toBe(false);
    // Only the hello handshake frame is on the wire.
    expect(h.sockets[0].sent.length).toBe(1);
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

  it("grows exponentially from a custom baseBackoffMs (TD-4801)", async () => {
    const h = buildClient({ baseBackoffMs: 5, maxBackoffMs: 1000 });
    await h.client.start();
    h.servers[0].handshake();

    // Drop: attempt 0 -> 5ms.
    h.servers[0].drop();
    await vi.advanceTimersByTimeAsync(5);
    await vi.advanceTimersByTimeAsync(0);
    expect(h.sockets.length).toBe(2);

    // Drop again: attempt 1 must be 10ms, not a flat 5ms. Under the old
    // precedence (`base ?? 500 * 2**attempt`) a configured base never grew,
    // so the second retry would already have fired by the 9ms mark.
    h.servers[1].drop();
    await vi.advanceTimersByTimeAsync(9);
    expect(h.sockets.length).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    await vi.advanceTimersByTimeAsync(0);
    expect(h.sockets.length).toBe(3);
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

  it("delivers an on-demand instruction_stack even when seq=1 is behind lastSeq (TD-1204)", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");
    h.servers[0].push(JSON.stringify({ type: "session_state", session_id: "sess-1", state: "running", seq: 1 }));
    expect(h.client.lastSeq("sess-1")).toBe(1);

    const before = h.onEvent.mock.calls.length;
    h.servers[0].push(
      JSON.stringify({
        type: "instruction_stack",
        session_id: "sess-1",
        seq: 1,
        sources: [],
        total_tokens: 0,
        token_method: "exact",
      }),
    );
    expect(h.onEvent).toHaveBeenCalledTimes(before + 1);
    expect(h.onEvent.mock.calls[before][0].type).toBe("instruction_stack");
    // Snapshot must not rewind or stall the log cursor.
    expect(h.client.lastSeq("sess-1")).toBe(1);
    h.client.stop();
  });

  it("still advances lastSeq when instruction_stack is the next logged event", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");
    h.servers[0].push(JSON.stringify({ type: "session_state", session_id: "sess-1", state: "running", seq: 1 }));
    h.servers[0].push(
      JSON.stringify({
        type: "instruction_stack",
        session_id: "sess-1",
        seq: 2,
        sources: [],
        total_tokens: 0,
        token_method: "exact",
      }),
    );
    expect(h.client.lastSeq("sess-1")).toBe(2);
    h.client.stop();
  });

  it("delivers design_hit even when seq=1 is behind lastSeq (TD-3403)", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");
    h.servers[0].push(JSON.stringify({ type: "session_state", session_id: "sess-1", state: "running", seq: 1 }));
    const before = h.onEvent.mock.calls.length;
    h.servers[0].push(
      JSON.stringify({
        type: "design_hit",
        session_id: "sess-1",
        seq: 1,
        x: 12,
        y: 34,
        xpath: "//button",
        role: "button",
        attributes: {},
        styles: {},
      }),
    );
    expect(h.onEvent).toHaveBeenCalledTimes(before + 1);
    expect(h.onEvent.mock.calls[before][0].type).toBe("design_hit");
    expect(h.client.lastSeq("sess-1")).toBe(1);
    h.client.stop();
  });

  it("log_trimmed jumps lastSeq so a windowed replay is not a false gap", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");

    h.servers[0].push(
      JSON.stringify({
        type: "log_trimmed",
        session_id: "sess-1",
        seq: 1,
        requested_from_seq: 1,
        earliest_seq: 8,
      }),
    );
    expect(h.onEvent).toHaveBeenCalledTimes(1);
    expect(h.onEvent.mock.calls[0][0].type).toBe("log_trimmed");
    expect(h.client.lastSeq("sess-1")).toBe(7);

    h.servers[0].push(
      JSON.stringify({ type: "assistant_delta", session_id: "sess-1", delta: "kept", seq: 8 }),
    );
    expect(h.client.lastSeq("sess-1")).toBe(8);
    expect(h.onEvent).toHaveBeenCalledTimes(2);
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
    h.client.stop();
  });
});

describe("attach-before-send (TD-1713)", () => {
  // 2026-08-14: a user_message reached the daemon for a session this client
  // was not attached to; events fan out only to attached connections, so the
  // UI waited forever. The client now attaches first, on the same socket.
  it("attaches before sending user_message to a session it is not following", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();

    const ok = h.client.send({ type: "user_message", session_id: "s2", content: "hello" });
    expect(ok).toBe(true);

    const frames = h.sockets[0].sent.map((raw) => JSON.parse(raw));
    const attachIdx = frames.findIndex((m) => m.type === "attach");
    const msgIdx = frames.findIndex((m) => m.type === "user_message");
    expect(attachIdx).toBeGreaterThanOrEqual(0);
    expect(attachIdx).toBeLessThan(msgIdx);
    expect(frames[attachIdx]).toEqual({ type: "attach", session_id: "s2", from_seq: 1 });
    expect(frames[msgIdx]).toEqual({ type: "user_message", session_id: "s2", content: "hello" });
    h.client.stop();
  });

  it("does not double-attach when the session is already followed", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("s1");
    h.sockets[0].sent.length = 0;

    h.client.send({ type: "user_message", session_id: "s1", content: "hi" });
    const frames = h.sockets[0].sent.map((raw) => JSON.parse(raw));
    expect(frames).toEqual([{ type: "user_message", session_id: "s1", content: "hi" }]);
    h.client.stop();
  });

  it("a refused send (not connected) does not silently register an attach", async () => {
    const h = buildClient();
    await h.client.start();
    h.sockets[0].onopen?.(); // hello out, no ack yet

    expect(h.client.send({ type: "user_message", session_id: "s2", content: "hi" })).toBe(false);
    expect(h.sockets[0].sent.length).toBe(1); // hello only — no attach, no message

    // After the handshake the retry must still attach first: had the failed
    // send registered s2, this send would skip the attach and re-open the gap.
    h.servers[0].ack();
    h.client.send({ type: "user_message", session_id: "s2", content: "hi" });
    const frames = h.sockets[0].sent.map((raw) => JSON.parse(raw));
    expect(frames[1]).toEqual({ type: "attach", session_id: "s2", from_seq: 1 });
    expect(frames[2]).toEqual({ type: "user_message", session_id: "s2", content: "hi" });
    h.client.stop();
  });

  it("an auto-attached session re-attaches after a reconnect", async () => {
    const h = buildClient({ baseBackoffMs: 5, maxBackoffMs: 8 });
    await h.client.start();
    h.servers[0].handshake();
    h.client.send({ type: "user_message", session_id: "s2", content: "hello" });

    h.servers[0].drop();
    await vi.advanceTimersByTimeAsync(5);
    const fresh = h.sockets[1];
    fresh.onopen?.();
    h.servers[1].ack();
    const frames = fresh.sent.map((raw) => JSON.parse(raw));
    expect(frames.some((m) => m.type === "attach" && m.session_id === "s2")).toBe(true);
    h.client.stop();
  });
});

describe("session control messages (TD-1006)", () => {
  it("sends open_workspace and set_tier after the handshake", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();

    h.client.openWorkspace("/Users/me/project");
    h.client.setTier("sess-1", "worker");

    const sent = h.sockets[0].sent.map((raw) => JSON.parse(raw));
    // [0] is the hello frame from the handshake.
    expect(sent[1]).toEqual({ type: "open_workspace", path: "/Users/me/project" });
    expect(sent[2]).toEqual({ type: "set_tier", session_id: "sess-1", tier: "worker" });
    h.client.stop();
  });

  it("does not send before the handshake completes", async () => {
    const h = buildClient();
    await h.client.start();
    h.sockets[0].onopen?.(); // hello sent, but no hello_ack yet

    h.client.openWorkspace("/tmp/x");
    h.client.setTier("sess-1", "brain");

    // Only the hello frame went out.
    expect(h.sockets[0].sent.length).toBe(1);
    h.client.stop();
  });

  it("accepts tier_state, boundary_update, shell_output, checkpoint_notice, context_compacted as known events", async () => {
    const h = buildClient();
    await h.client.start();
    h.servers[0].handshake();
    h.client.attach("sess-1");

    const events = [
      { type: "session_state", session_id: "sess-1", state: "running", seq: 1 },
      {
        type: "boundary_update", session_id: "sess-1", seq: 2,
        writable_paths: ["**"], allowed_commands: [], network: "deny",
        spend_usd: 25, wall_clock_hours: 8, max_iterations: 200, source: "defaults",
      },
      {
        type: "tier_state", session_id: "sess-1", seq: 3, tier: "brain", override: null,
        model_slugs: { brain: "b", worker: "w", validator: "v" },
      },
      { type: "shell_output", session_id: "sess-1", tool_call_id: "tc-1", stream: "stdout", chunk: "hi\n", seq: 4 },
      { type: "checkpoint_notice", session_id: "sess-1", code: "no_git", message: "m", seq: 5 },
      {
        type: "context_compacted", session_id: "sess-1", seq: 6,
        dropped_messages: 4, kept_messages: 2, tokens_before: 900, tokens_after: 500,
      },
    ];
    for (const e of events) h.servers[0].push(JSON.stringify(e));

    // Every one reached the sink, none warned as unknown.
    expect(h.onEvent).toHaveBeenCalledTimes(events.length);
    expect(h.client.lastSeq("sess-1")).toBe(6);
    h.client.stop();
  });
});
