// Resume healing regression tests (TD-1716).
//
// Reproduces the 2026-08-15 twenty-minute "Whittling…": macOS suspended the
// occluded WKWebView's JavaScript while the socket stayed open and healthy.
// The daemon answered two turns into it; the frames never reached the page,
// the 25s watchdog never fired, and the UI sat frozen mid-frame with no error
// anywhere and no way for the user to un-stick it.
//
// The harness models a suspension exactly as the OS delivers one:
//   - every inbound frame is dropped (the page's event loop is not running)
//   - timers are frozen while the wall clock advances (vi.setSystemTime moves
//     `now` and pushes pending timers along with it, so nothing fires and the
//     backlog arrives coalesced, which is what defeated the watchdog)
// Nothing else is stubbed: the real ProtocolClient talks to a fake daemon that
// keeps a real per-session event log and answers `attach` with a real replay,
// and the real chat store reduces what comes back.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ProtocolClient, ZOMBIE_SILENCE_MS, type SocketLike } from "./client";
import { createChatState, createChatStore, STALL_TIMEOUT_MS } from "./chat-store";
import { watchResume, type ResumeDocument, type ResumeWindow } from "./resume";
import type { ClientMessageUnion, DaemonEventUnion } from "./protocol";

const SESSION = "s1";

class FakeSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(private readonly onSend: (raw: string) => void) {}

  send(raw: string): void {
    this.sent.push(raw);
    this.onSend(raw);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.();
  }
}

/**
 * A daemon with a per-session event log, an attach replay, and a suspend
 * switch. `suspended` drops frames on the floor rather than queueing them:
 * the healing must not depend on the OS having buffered anything, since in the
 * zombie case it did not.
 */
class FakeDaemon {
  socket: FakeSocket | null = null;
  suspended = false;
  readonly received: ClientMessageUnion[] = [];
  private readonly logs = new Map<string, Record<string, unknown>[]>();

  /** Wire a freshly built socket up as the live connection. */
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
    if (this.suspended || this.socket === null) return;
    this.socket.onmessage?.({ data: JSON.stringify(frame) });
  }

  /** Append to the session's log (assigning the next seq) and stream it. */
  emit(sessionId: string, event: Record<string, unknown>): void {
    const log = this.log(sessionId);
    const frame = { ...event, session_id: sessionId, seq: log.length + 1 };
    log.push(frame);
    this.deliver(frame);
  }

  /** The application-level liveness frame (no session, no seq). */
  ping(): void {
    this.deliver({ type: "ping" });
  }

  lastSeq(sessionId: string): number {
    return this.log(sessionId).length;
  }

  receive(raw: string): void {
    const msg = JSON.parse(raw) as ClientMessageUnion;
    this.received.push(msg);
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

  attachesFor(sessionId: string): { session_id: string; from_seq: number }[] {
    return this.received
      .filter((m): m is Extract<ClientMessageUnion, { type: "attach" }> => m.type === "attach")
      .filter((m) => m.session_id === sessionId);
  }
}

interface Harness {
  daemon: FakeDaemon;
  client: ProtocolClient;
  state: ReturnType<typeof createChatState>;
  store: ReturnType<typeof createChatStore>;
  sockets: FakeSocket[];
  /** Events the client handed to the app (pings must never appear here). */
  seen: DaemonEventUnion[];
  /** The page is running: timers fire and the clock moves with them. */
  advance(ms: number): void;
  /** The page is suspended: frames dropped, timers frozen, clock moves on. */
  suspend(ms: number): void;
  /** visibilitychange → visible / window focus, as connection-status wires it. */
  resume(): void;
  dispose(): void;
}

/** Flush the microtask queue — `start()` and the reconnect path both await. */
async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) await Promise.resolve();
}

/** A connected client with the chat pane bound and attached to SESSION. */
async function boot(): Promise<Harness> {
  const daemon = new FakeDaemon();
  const sockets: FakeSocket[] = [];
  const seen: DaemonEventUnion[] = [];
  const state = createChatState();

  const client = new ProtocolClient(
    {
      async getDaemonInfo() {
        return { port: 9000, token: "t" };
      },
      socketFactory() {
        const socket = new FakeSocket((raw) => daemon.receive(raw));
        sockets.push(socket);
        daemon.connect(socket);
        // The transport opens on its own, as a real one does; the client has
        // its handlers on the socket by the time this microtask runs.
        queueMicrotask(() => socket.onopen?.());
        return socket;
      },
    },
    {
      onEvent(event) {
        seen.push(event);
        store.applyEvent(event);
      },
    },
  );

  const store = createChatStore(
    {
      send: (msg) => client.send(msg),
      attach: (id) => client.attach(id),
      detach: (id) => client.detach(id),
    },
    state,
  );

  await client.start();
  await flush();

  // The session exists and is live before the pane binds to it.
  daemon.emit(SESSION, { type: "session_state", state: "running" });
  store.selectSession(SESSION, null);

  return {
    daemon,
    client,
    state,
    store,
    sockets,
    seen,
    advance(ms) {
      vi.advanceTimersByTime(ms);
    },
    suspend(ms) {
      daemon.suspended = true;
      // Moves `now` and drags every pending timer along by the same amount:
      // nothing fires, and what was scheduled is still ahead of us. That is a
      // suspended JS engine, and it is why the watchdog cannot be trusted on
      // the way back.
      vi.setSystemTime(Date.now() + ms);
    },
    resume() {
      daemon.suspended = false;
      // The order connection-status.handleResume uses: socket first, so its
      // re-attach frames are already in flight, then the stores.
      client.resume();
      store.resume();
    },
    dispose() {
      store.dispose();
      client.stop();
    },
  };
}

describe("resume healing: re-attach after suspension (TD-1716)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("re-attaches every followed session at lastSeq+1 and applies the replay", async () => {
    const h = await boot();
    const attachesBefore = h.daemon.attachesFor(SESSION).length;

    // Suspended: the daemon answers a whole turn into a socket whose reader
    // is not running. Every frame is lost.
    h.suspend(30_000);
    h.daemon.emit(SESSION, { type: "assistant_delta", delta: "answered while you slept" });
    h.daemon.emit(SESSION, {
      type: "turn_complete",
      tokens: 12,
      cost: 0,
      tier: "brain",
      duration: 2,
      failed: false,
      error_code: null,
    });
    expect(h.state.messages).toHaveLength(0);

    h.resume();

    const attaches = h.daemon.attachesFor(SESSION);
    expect(attaches).toHaveLength(attachesBefore + 1);
    // lastSeq+1: seq 1 (the session_state seen before the suspension) + 1.
    expect(attaches.at(-1)?.from_seq).toBe(2);

    // The replay closed the gap with no user action.
    expect(h.state.messages.at(-1)?.text).toBe("answered while you slept");
    expect(h.client.lastSeq(SESSION)).toBe(h.daemon.lastSeq(SESSION));
    h.dispose();
  });

  it("re-attaches unconditionally — a suspension with nothing missed still heals", async () => {
    const h = await boot();
    const before = h.daemon.attachesFor(SESSION).length;

    h.suspend(1_000);
    h.resume();

    // The client cannot know whether it missed anything, so it never decides
    // it didn't: the replay of an empty gap costs one frame.
    expect(h.daemon.attachesFor(SESSION)).toHaveLength(before + 1);
    expect(h.sockets).toHaveLength(1);
    h.dispose();
  });
});

describe("resume healing: the zombie socket (TD-1716)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("force-closes a socket silent past the threshold, then reconnects and replays", async () => {
    const h = await boot();

    // Long enough that even the pings are gone: the socket looks connected and
    // is not. The transport can never report this — the OS answers its
    // ping/pong on the frozen page's behalf.
    h.suspend(ZOMBIE_SILENCE_MS + 15_000);
    h.daemon.emit(SESSION, { type: "assistant_delta", delta: "lost to the zombie" });

    h.resume();
    await flush();

    expect(h.sockets[0].closed).toBe(true);
    expect(h.sockets).toHaveLength(2);
    expect(h.client.connectionState).toBe("connected");

    // The existing reconnect path did the re-attach on its hello_ack, and the
    // replay delivered what the dead socket swallowed.
    const attaches = h.daemon.attachesFor(SESSION);
    expect(attaches.at(-1)?.from_seq).toBe(2);
    expect(h.state.messages.at(-1)?.text).toBe("lost to the zombie");
    h.dispose();
  });

  it("only one reconnect comes out of a zombie close, not a retry storm", async () => {
    const h = await boot();
    h.suspend(ZOMBIE_SILENCE_MS + 1_000);
    h.resume();
    await flush();

    // The dead socket's late `onclose` must not schedule a retry on top of the
    // reconnect already in flight.
    h.advance(60_000);
    await flush();
    expect(h.sockets).toHaveLength(2);
    h.dispose();
  });

  it("a client kept fresh by pings is not a zombie: it re-attaches in place", async () => {
    const h = await boot();

    // Awake but idle for well past the threshold — the daemon's ~15s ping is
    // the only traffic. Each one is proof this page's JS is running.
    for (let i = 0; i < 4; i++) {
      h.advance(15_000);
      h.daemon.ping();
    }
    expect(Date.now()).toBeGreaterThan(ZOMBIE_SILENCE_MS);

    h.resume();
    await flush();

    expect(h.sockets).toHaveLength(1);
    expect(h.sockets[0].closed).toBe(false);
    expect(h.daemon.attachesFor(SESSION).at(-1)?.from_seq).toBe(2);
    h.dispose();
  });

  it("the ping never reaches a store: no session, no seq, nothing to reduce", async () => {
    const h = await boot();
    const seenBefore = h.seen.length;
    const seqBefore = h.client.lastSeq(SESSION);

    h.daemon.ping();

    expect(h.seen).toHaveLength(seenBefore);
    expect(h.client.lastSeq(SESSION)).toBe(seqBefore);
    expect(h.state.messages).toHaveLength(0);
    h.dispose();
  });
});

describe("resume healing: the first-token watchdog is honest (TD-1716)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("flips to the stalled copy immediately when the wait outlived the threshold", async () => {
    const h = await boot();
    expect(h.store.sendUserMessage("hello")).toBe(true);
    expect(h.state.awaitingFirstToken).toBe(true);

    // Twenty minutes of "Whittling…": the timer was frozen through all of it,
    // so on its own it still believes the wait has 25s to run.
    h.suspend(20 * 60_000);
    expect(h.state.turnStalled).toBe(false);

    h.resume();

    // Immediately — no timer was advanced between the resume and this line.
    expect(h.state.turnStalled).toBe(true);
    expect(h.state.awaitingFirstToken).toBe(true); // the wait is honest, not over
    h.dispose();
  });

  it("a short suspension re-arms for the remainder instead of stalling early", async () => {
    const h = await boot();
    h.store.sendUserMessage("hello");

    h.suspend(10_000);
    h.resume();
    expect(h.state.turnStalled).toBe(false);

    h.advance(STALL_TIMEOUT_MS - 10_000 - 1);
    expect(h.state.turnStalled).toBe(false);
    h.advance(1);
    expect(h.state.turnStalled).toBe(true);
    h.dispose();
  });

  it("the replayed first token clears a stall the suspension caused", async () => {
    const h = await boot();
    h.store.sendUserMessage("hello");

    // Long enough to be a zombie as well as a stall — the shape of the real
    // 2026-08-15 incident. The daemon answered while the page slept and the
    // frame went nowhere.
    h.suspend(ZOMBIE_SILENCE_MS + STALL_TIMEOUT_MS);
    h.daemon.emit(SESSION, { type: "assistant_delta", delta: "here you go" });

    h.resume();
    // Honest the instant we look: the wait really has outlived the threshold.
    expect(h.state.turnStalled).toBe(true);

    await flush();

    // …and the replay that the reconnect fetched clears it. The user ends up
    // reading the answer, not the apology.
    expect(h.state.turnStalled).toBe(false);
    expect(h.state.awaitingFirstToken).toBe(false);
    expect(h.state.messages.at(-1)?.text).toBe("here you go");
    h.dispose();
  });

  it("resume is a no-op when nothing is waiting", async () => {
    const h = await boot();
    h.suspend(60 * 60_000);
    h.resume();

    expect(h.state.turnStalled).toBe(false);
    expect(h.state.awaitingFirstToken).toBe(false);
    h.dispose();
  });
});

describe("resume detection (TD-1716)", () => {
  function fakeTargets() {
    const listeners = new Map<string, (() => void)[]>();
    const add = (type: string, handler: () => void): void => {
      listeners.set(type, [...(listeners.get(type) ?? []), handler]);
    };
    const remove = (type: string, handler: () => void): void => {
      listeners.set(type, (listeners.get(type) ?? []).filter((h) => h !== handler));
    };
    const doc = {
      visibilityState: "visible",
      addEventListener: add,
      removeEventListener: remove,
    } as ResumeDocument & { visibilityState: string };
    const win = { addEventListener: add, removeEventListener: remove } as ResumeWindow;
    const fire = (type: string): void => {
      for (const handler of listeners.get(type) ?? []) handler();
    };
    return { doc, win, fire };
  }

  it("heals on visibilitychange → visible and on window focus", () => {
    const { doc, win, fire } = fakeTargets();
    const onResume = vi.fn();
    watchResume(doc, win, onResume);

    fire("visibilitychange");
    expect(onResume).toHaveBeenCalledTimes(1);

    fire("focus");
    expect(onResume).toHaveBeenCalledTimes(2);
  });

  it("ignores the hidden edge — healing into a socket about to sleep is wasted", () => {
    const { doc, win, fire } = fakeTargets();
    const onResume = vi.fn();
    watchResume(doc, win, onResume);

    doc.visibilityState = "hidden";
    fire("visibilitychange");
    expect(onResume).not.toHaveBeenCalled();
  });

  it("unsubscribes both listeners", () => {
    const { doc, win, fire } = fakeTargets();
    const onResume = vi.fn();
    const stop = watchResume(doc, win, onResume);

    stop();
    fire("visibilitychange");
    fire("focus");
    expect(onResume).not.toHaveBeenCalled();
  });
});
