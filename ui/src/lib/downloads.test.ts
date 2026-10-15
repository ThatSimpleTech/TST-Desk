// The pull engine (TD-5144, design §5.2), against a fake sink, a fake socket
// and fake timers: the window, ordering, resume, stalls, the connection,
// the codes that land on the card, the queue and keep-awake.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ArtifactChunk, ClientMessageUnion, DaemonEventUnion, MachineStatus } from "./protocol";

const bus = vi.hoisted(() => ({
  events: new Set<(event: DaemonEventUnion) => void>(),
  states: new Set<(state: string) => void>(),
  resumes: new Set<() => void>(),
}));

vi.mock("./connection-status.svelte.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
  const sub =
    <T,>(set: Set<T>) =>
    (handler: T) => {
      set.add(handler);
      return () => void set.delete(handler);
    };
  return {
    ...real,
    onDaemonEvent: sub(bus.events),
    onConnectionState: sub(bus.states),
    onResume: sub(bus.resumes),
    sendToDaemon: () => false,
  };
});

vi.mock("./machines.svelte.js", () => ({ machineNameFor: () => "studio-mac", machineById: () => null }));

import {
  CHUNK,
  configureDownloads,
  downloads,
  MAX_STRIKES,
  pauseDownload,
  resetDownloads,
  resumeDownload,
  runAction,
  startDownload,
  stateFor,
  STALL_MS,
  TICK_MS,
  WINDOW,
} from "./downloads.svelte";
import { SinkError, type FileHost, type FileSink, type FinishResult, type SinkStatus } from "./file-sink";
import { claimedInline } from "./inline-errors";
import { fileActions, fileKey, fileKind, type SentFileRef } from "./sent-files";

const SHA = "ab".repeat(32);

/** A sink that counts bytes without holding them: each chunk is its offset. */
class FakeSink implements FileSink {
  have = 0;
  appended: number[] = [];
  beginState: "none" | "partial" | "verified" = "none";
  finishes: FinishResult[] = [];
  finished = 0;
  discards = 0;
  statusOverride: SinkStatus | null = null;

  constructor(readonly size: number) {}

  async begin(): Promise<{ have: number; state: "none" | "partial" | "verified" }> {
    return { have: this.beginState === "verified" ? this.size : this.have, state: this.beginState };
  }
  async append(offset: number, data: string): Promise<number> {
    if (offset !== this.have) throw new SinkError("bad_offset", this.have);
    expect(data).toBe(`chunk@${offset}`);
    this.appended.push(offset);
    this.have = Math.min(this.size, offset + CHUNK);
    return this.have;
  }
  async finish(): Promise<FinishResult> {
    this.finished += 1;
    return this.finishes.shift() ?? { ok: true };
  }
  async status(): Promise<SinkStatus> {
    if (this.statusOverride !== null) return this.statusOverride;
    if (this.have >= this.size && this.finished > 0) return { state: "verified", have: this.size };
    return { state: this.have > 0 ? "partial" : "none", have: this.have };
  }
  async discard(): Promise<void> {
    this.discards += 1;
    this.have = 0;
  }
}

let sinks: Map<string, FakeSink>;
let sent: ClientMessageUnion[];
let online: boolean;
/** What the machines store lists, by handle. */
let listed: Record<string, MachineStatus>;
let host: FileHost & {
  keepAwake: ReturnType<typeof vi.fn<(on: boolean) => void>>;
  open: ReturnType<typeof vi.fn<(ref: SentFileRef) => Promise<void>>>;
  saveToDownloads: ReturnType<typeof vi.fn<(ref: SentFileRef) => Promise<string>>>;
};

function file(n: number, size = 6 * CHUNK, over: Partial<SentFileRef> = {}): SentFileRef {
  return {
    artifactId: n.toString(16).padStart(32, "0"),
    sessionId: "sess-1",
    name: `file-${n}.bin`,
    size,
    mime: "application/octet-stream",
    sha256: SHA,
    caption: null,
    toolCallId: "call_1",
    ...over,
  };
}

function sinkOf(ref: SentFileRef): FakeSink {
  let sink = sinks.get(ref.artifactId);
  if (sink === undefined) {
    sink = new FakeSink(ref.size);
    sinks.set(ref.artifactId, sink);
  }
  return sink;
}

function requests(ref?: SentFileRef): number[] {
  return sent.flatMap((m) =>
    m.type === "read_artifact_chunk" && (ref === undefined || m.artifact_id === ref.artifactId) ? [m.offset] : [],
  );
}

function emit(event: DaemonEventUnion): void {
  for (const handler of [...bus.events]) handler(event);
}

function chunk(ref: SentFileRef, offset: number, over: Partial<ArtifactChunk> = {}): ArtifactChunk {
  return {
    type: "artifact_chunk",
    seq: 0,
    session_id: ref.sessionId,
    artifact_id: ref.artifactId,
    offset,
    size: ref.size,
    sha256: ref.sha256,
    data_b64: `chunk@${offset}`,
    eof: offset + CHUNK >= ref.size,
    error_code: null,
    ...over,
  };
}

async function flush(): Promise<void> {
  for (let i = 0; i < 30; i++) await Promise.resolve();
}

async function reply(ref: SentFileRef, offset: number, over: Partial<ArtifactChunk> = {}): Promise<void> {
  emit(chunk(ref, offset, over));
  await flush();
}

/** Answer every request until the file is in. */
async function serve(ref: SentFileRef): Promise<void> {
  for (let guard = 0; guard < 100; guard++) {
    const phase = downloads[fileKey(ref)]?.phase;
    if (phase !== "downloading") return;
    const sink = sinkOf(ref);
    const out = requests(ref).filter((o) => o >= sink.have);
    if (out.length === 0) return;
    await reply(ref, Math.min(...out));
  }
}

function state(ref: SentFileRef) {
  return downloads[fileKey(ref)];
}

beforeEach(() => {
  vi.useFakeTimers();
  bus.events.clear();
  bus.states.clear();
  bus.resumes.clear();
  sinks = new Map();
  sent = [];
  online = true;
  listed = {};
  host = {
    platform: "android",
    sink: (ref: SentFileRef) => sinkOf(ref),
    open: vi.fn(async (_ref: SentFileRef) => {}),
    reveal: vi.fn(async () => {}),
    saveAs: vi.fn(async () => null),
    saveToDownloads: vi.fn(async (ref: SentFileRef) => `Download/TST Desk/${ref.name}`),
    remove: vi.fn(async () => {}),
    keepAwake: vi.fn<(on: boolean) => void>(),
  };
  resetDownloads();
  configureDownloads({
    platform: "android",
    host,
    send: (msg) => {
      if (!online) return false;
      sent.push(msg);
      return true;
    },
    machine: () => "studio-mac",
    hubName: () => "office-pc",
    machineStatus: (id) => listed[id] ?? null,
  });
});

afterEach(() => {
  resetDownloads();
  vi.useRealTimers();
});

describe("the window", () => {
  it("keeps three 512 KiB requests out, and asks for the next as each lands", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    expect(WINDOW).toBe(3);
    expect(requests()).toEqual([0, CHUNK, 2 * CHUNK]);
    expect(sent[0]).toEqual({
      type: "read_artifact_chunk",
      session_id: "sess-1",
      artifact_id: ref.artifactId,
      offset: 0,
      length: 524288,
    });
    await reply(ref, 0);
    expect(requests()).toEqual([0, CHUNK, 2 * CHUNK, 3 * CHUNK]);
    expect(state(ref).phase).toBe("downloading");
    expect(state(ref).have).toBe(CHUNK);
  });

  it("appends in order when replies arrive out of order, and drops duplicates", async () => {
    const ref = file(1, 3 * CHUNK);
    startDownload(ref);
    await flush();
    await reply(ref, 2 * CHUNK);
    await reply(ref, CHUNK);
    expect(sinkOf(ref).appended).toEqual([]);
    // A reply waiting its turn still counts against the window.
    expect(requests()).toEqual([0, CHUNK, 2 * CHUNK]);
    await reply(ref, 0);
    await reply(ref, 0);
    await reply(ref, CHUNK);
    expect(sinkOf(ref).appended).toEqual([0, CHUNK, 2 * CHUNK]);
    expect(state(ref).phase).toBe("done");
  });

  it("finishes a size-0 file without asking for a byte", async () => {
    const ref = file(1, 0, { sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" });
    startDownload(ref);
    await flush();
    expect(requests()).toEqual([]);
    expect(sinkOf(ref).finished).toBe(1);
    expect(state(ref).phase).toBe("done");
  });
});

describe("resume", () => {
  it("carries on from the part the sink already holds", async () => {
    const ref = file(1);
    const sink = sinkOf(ref);
    sink.have = 4 * CHUNK;
    sink.beginState = "partial";
    expect(stateFor(ref).phase).toBe("checking");
    await flush();
    expect(state(ref)).toMatchObject({ phase: "paused", have: 4 * CHUNK, partial: true, waiting: false });

    await resumeDownload(ref);
    await flush();
    expect(requests()).toEqual([4 * CHUNK, 5 * CHUNK]);
  });

  it("asks the sink where to carry on after a pause", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    await reply(ref, 0);
    pauseDownload(ref);
    expect(state(ref)).toMatchObject({ phase: "paused", have: CHUNK, waiting: false });
    sent = [];
    await resumeDownload(ref);
    await flush();
    expect(requests()).toEqual([CHUNK, 2 * CHUNK, 3 * CHUNK]);
    // A reply from before the pause is not ours any more.
    await reply(ref, 0);
    expect(sinkOf(ref).appended).toEqual([0]);
  });

  it("treats a size or sha change as a changed file", async () => {
    const a = file(1);
    startDownload(a);
    await flush();
    await reply(a, 0, { size: a.size + 1 });
    expect(state(a)).toMatchObject({ phase: "failed", note: "The file changed on studio-mac. Ask for it again." });

    const b = file(2);
    startDownload(b);
    await flush();
    await reply(b, 0, { sha256: "cd".repeat(32) });
    expect(state(b)).toMatchObject({ phase: "failed", code: "changed" });
  });
});

describe("stalls and the connection", () => {
  it("asks again after 20 s, and pauses after three strikes", async () => {
    const ref = file(1, CHUNK);
    startDownload(ref);
    await flush();
    expect(requests()).toEqual([0]);
    await vi.advanceTimersByTimeAsync(STALL_MS - TICK_MS);
    expect(requests()).toEqual([0]);
    await vi.advanceTimersByTimeAsync(TICK_MS);
    expect(requests()).toEqual([0, 0]);
    await vi.advanceTimersByTimeAsync(STALL_MS * (MAX_STRIKES - 1));
    expect(requests()).toEqual([0, 0, 0, 0]);
    expect(state(ref).phase).toBe("downloading");
    await vi.advanceTimersByTimeAsync(STALL_MS);
    expect(state(ref)).toMatchObject({ phase: "paused", waiting: true });
    expect(requests()).toHaveLength(1 + MAX_STRIKES);
  });

  it("pauses on a dropped connection and resumes when it is back", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    await reply(ref, 0);
    for (const handler of bus.states) handler("reconnecting");
    expect(state(ref)).toMatchObject({ phase: "paused", waiting: true, have: CHUNK });

    sent = [];
    for (const handler of bus.states) handler("connected");
    await flush();
    expect(state(ref).phase).toBe("downloading");
    expect(requests()).toEqual([CHUNK, 2 * CHUNK, 3 * CHUNK]);
  });

  it("pauses when the socket refuses the request, and onResume picks it up", async () => {
    const ref = file(1);
    online = false;
    startDownload(ref);
    await flush();
    expect(state(ref)).toMatchObject({ phase: "paused", waiting: true });
    online = true;
    for (const handler of bus.resumes) handler();
    await flush();
    expect(requests()).toEqual([0, CHUNK, 2 * CHUNK]);
  });
});

describe("codes on the card", () => {
  it("restarts once on a hash mismatch, then fails", async () => {
    const ref = file(1, CHUNK);
    const sink = sinkOf(ref);
    sink.finishes = [
      { ok: false, code: "hash_mismatch" },
      { ok: false, code: "hash_mismatch" },
    ];
    startDownload(ref);
    await flush();
    await serve(ref);
    expect(sink.discards).toBe(1);
    expect(requests()).toEqual([0, 0]);
    await serve(ref);
    expect(sink.finished).toBe(2);
    expect(state(ref)).toMatchObject({ phase: "failed", note: "The download was damaged. Try again." });
  });

  it("artifact_not_found: the file is gone", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    await reply(ref, 0, { error_code: "artifact_not_found", data_b64: "" });
    expect(state(ref)).toMatchObject({ phase: "gone", note: "This file is no longer on studio-mac." });
  });

  it("gone after part of the file is in: the phone offers to remove the part", async () => {
    const ref = file(1, 6 * CHUNK);
    startDownload(ref);
    await flush();
    await reply(ref, 0);
    await reply(ref, CHUNK);
    expect(sinkOf(ref).have).toBe(2 * CHUNK);
    await reply(ref, 2 * CHUNK, { error_code: "artifact_not_found", data_b64: "" });
    expect(state(ref)).toMatchObject({ phase: "gone", partial: true });
    expect(fileActions("android", fileKind(ref.name, ref.mime), "gone", { partial: state(ref).partial })).toEqual({
      primary: null,
      menu: ["remove"],
    });
  });

  it("an older daemon's unknown_message for the verb: unavailable, claimed inline", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    const other: DaemonEventUnion = { type: "error", seq: 0, code: "unknown_message", message: "unknown message type: design_hit" };
    emit(other);
    expect(claimedInline(other)).toBe(false);
    expect(state(ref).phase).toBe("downloading");

    const event: DaemonEventUnion = {
      type: "error",
      seq: 0,
      code: "unknown_message",
      message: "unknown message type: read_artifact_chunk",
    };
    emit(event);
    expect(claimedInline(event)).toBe(true);
    // The hub is the old one: the computer that sent the file is new (§4.5).
    expect(state(ref)).toMatchObject({
      phase: "unavailable",
      note: "Update TST Desk on office-pc to download files from studio-mac.",
    });
  });

  it("an unknown_message relayed from the other computer names that computer", async () => {
    const ref = file(1, CHUNK);
    startDownload(ref);
    await flush();
    emit({
      type: "error",
      seq: 0,
      code: "unknown_message",
      message: "On studio-mac: unknown message type: read_artifact_chunk",
      machine: "mch_1a2b3c4d5e6f",
    });
    expect(state(ref)).toMatchObject({ phase: "unavailable", note: "Update TST Desk on studio-mac to download this." });
  });

  it("a hub refusal for this session lands on the card", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    const other: DaemonEventUnion = { type: "error", seq: 0, session_id: "sess-9", code: "not_on_remote", message: "x" };
    emit(other);
    expect(claimedInline(other)).toBe(false);
    const event: DaemonEventUnion = { type: "error", seq: 0, session_id: "sess-1", code: "not_on_remote", message: "x" };
    emit(event);
    expect(claimedInline(event)).toBe(true);
    expect(state(ref)).toMatchObject({
      phase: "unavailable",
      note: "Update TST Desk on office-pc to download files from studio-mac.",
    });
  });
});

describe("the queue", () => {
  it("runs one download at a time, first come first served", async () => {
    const a = file(1, CHUNK);
    const b = file(2, CHUNK);
    const c = file(3, CHUNK);
    startDownload(a);
    startDownload(b);
    startDownload(c);
    await flush();
    expect([state(a).phase, state(b).phase, state(c).phase]).toEqual(["downloading", "queued", "queued"]);
    expect(requests(b)).toEqual([]);

    await serve(a);
    expect(state(a).phase).toBe("done");
    expect(state(b).phase).toBe("downloading");
    expect(state(c).phase).toBe("queued");
    await serve(b);
    expect(state(c).phase).toBe("downloading");
    expect(sent.map((m) => (m.type === "read_artifact_chunk" ? m.artifact_id : "")).map((id) => id.at(-1))).toEqual([
      "1",
      "2",
      "3",
    ]);
  });
});

const MCH = "mch_1a2b3c4d5e6f";
const BRIDGED = `sess-1:${MCH}`;

function unreachable(sessionId = BRIDGED): DaemonEventUnion {
  return {
    type: "error",
    seq: 0,
    session_id: sessionId,
    code: "machine_unreachable",
    message: "studio-mac isn't reachable right now. Try again when it is.",
    machine: MCH,
  };
}

function machineList(entries: Record<string, MachineStatus>): DaemonEventUnion {
  return {
    type: "machine_list",
    seq: 0,
    machines: Object.entries(entries).map(([id, status]) => ({
      id,
      name: "studio-mac",
      host: null,
      port: null,
      status,
      last_seen: null,
      paired_at: "2026-10-01T00:00:00Z",
      same_computer: false,
      detail: null,
    })),
  };
}

describe("the other computer goes away (machine_unreachable)", () => {
  const COPY = "studio-mac isn't reachable. The download carries on when it is.";

  it("is claimed for its download, pauses with the copy, and carries on when the computer is back", async () => {
    const ref = file(1, 6 * CHUNK, { sessionId: BRIDGED });
    startDownload(ref);
    await flush();
    await reply(ref, 0);
    const event = unreachable();
    emit(event);
    expect(claimedInline(event)).toBe(true);
    expect(state(ref)).toMatchObject({ phase: "paused", have: CHUNK, note: COPY, code: "machine_unreachable" });

    sent = [];
    emit(machineList({ mch_ffffffffffff: "connected", [MCH]: "unreachable" }));
    await flush();
    expect(state(ref).phase).toBe("paused");
    expect(requests()).toEqual([]);

    emit(machineList({ [MCH]: "connected" }));
    await flush();
    expect(state(ref)).toMatchObject({ phase: "downloading", note: null });
    expect(requests()).toEqual([CHUNK, 2 * CHUNK, 3 * CHUNK]);
  });

  it("is not claimed for another session's download", async () => {
    const ref = file(1, 6 * CHUNK, { sessionId: BRIDGED });
    startDownload(ref);
    await flush();
    const other = unreachable(`sess-9:${MCH}`);
    emit(other);
    expect(claimedInline(other)).toBe(false);
    expect(state(ref).phase).toBe("downloading");
  });

  it("asks again on its own while the list already says connected, three times at most", async () => {
    listed[MCH] = "connected";
    const ref = file(1, 6 * CHUNK, { sessionId: BRIDGED });
    startDownload(ref);
    await flush();
    for (let round = 0; round < MAX_STRIKES; round++) {
      emit(unreachable());
      expect(state(ref).phase).toBe("paused");
      sent = [];
      await vi.advanceTimersByTimeAsync(STALL_MS);
      expect(state(ref).phase, `round ${round}`).toBe("downloading");
      expect(requests()).toEqual([0, CHUNK, 2 * CHUNK]);
    }
    emit(unreachable());
    sent = [];
    await vi.advanceTimersByTimeAsync(STALL_MS * 3);
    expect(state(ref)).toMatchObject({ phase: "paused", note: COPY });
    expect(requests()).toEqual([]);
  });

  it("Cancel stops the wait for the computer", async () => {
    const ref = file(1, 6 * CHUNK, { sessionId: BRIDGED });
    startDownload(ref);
    await flush();
    emit(unreachable());
    await runAction(ref, "cancel");
    sent = [];
    emit(machineList({ [MCH]: "connected" }));
    await flush();
    expect(state(ref).phase).toBe("idle");
    expect(requests()).toEqual([]);
  });
});

describe("the rest of the window after a refusal", () => {
  function oldHub(): DaemonEventUnion {
    return { type: "error", seq: 0, code: "unknown_message", message: "unknown message type: read_artifact_chunk" };
  }

  function hubRefusal(sessionId = "sess-1"): DaemonEventUnion {
    return { type: "error", seq: 0, session_id: sessionId, code: "not_on_remote", message: "x" };
  }

  it("claims all three replies an old hub sends, not just the first", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    const replies = [oldHub(), oldHub(), oldHub()];
    for (const event of replies) emit(event);
    expect(replies.map((e) => claimedInline(e))).toEqual([true, true, true]);
    expect(state(ref).phase).toBe("unavailable");
  });

  it("claims all three of the hub's refusals, and of its machine_unreachable", async () => {
    const a = file(1);
    startDownload(a);
    await flush();
    const refusals = [hubRefusal(), hubRefusal(), hubRefusal()];
    for (const event of refusals) emit(event);
    expect(refusals.map((e) => claimedInline(e))).toEqual([true, true, true]);

    const b = file(2, 6 * CHUNK, { sessionId: BRIDGED });
    startDownload(b);
    await flush();
    const gone = [unreachable(), unreachable(), unreachable()];
    for (const event of gone) emit(event);
    expect(gone.map((e) => claimedInline(e))).toEqual([true, true, true]);
    expect(state(b).phase).toBe("paused");
  });

  it("a late refusal from the first download never lands on the next one", async () => {
    const a = file(1);
    const b = file(2);
    startDownload(a);
    startDownload(b);
    await flush();
    emit(oldHub());
    await flush();
    expect(state(b).phase).toBe("downloading");
    emit(oldHub());
    emit(oldHub());
    await flush();
    expect(state(b).phase).toBe("downloading");
    const own = [oldHub(), oldHub(), oldHub()];
    for (const event of own) emit(event);
    expect(own.map((e) => claimedInline(e))).toEqual([true, true, true]);
    expect(state(b).phase).toBe("unavailable");
  });

  it("claims the replies to what was out when the user paused, and nothing after", async () => {
    const ref = file(1, 6 * CHUNK, { sessionId: BRIDGED });
    startDownload(ref);
    await flush();
    pauseDownload(ref);
    const late = [unreachable(), unreachable(), unreachable(), unreachable()];
    for (const event of late) emit(event);
    expect(late.map((e) => claimedInline(e))).toEqual([true, true, true, false]);
    expect(state(ref)).toMatchObject({ phase: "paused", note: null });
  });

  it("owes nothing for a dropped socket, so a fresh refusal after the reconnect lands", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    for (const handler of bus.states) handler("reconnecting");
    for (const handler of bus.states) handler("connected");
    await flush();
    const fresh = hubRefusal();
    emit(fresh);
    expect(claimedInline(fresh)).toBe(true);
    expect(state(ref).phase).toBe("unavailable");
  });

  it("stops claiming once a stall's wait has passed", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    emit(hubRefusal());
    await vi.advanceTimersByTimeAsync(STALL_MS + 1);
    const later = hubRefusal();
    emit(later);
    expect(claimedInline(later)).toBe(false);
  });
});

describe("a paused download and the slot", () => {
  it("a hub pause hands the slot on, so the next download starts", async () => {
    const a = file(1);
    const b = file(2, CHUNK, { sessionId: "sess-2" });
    startDownload(a);
    await flush();
    emit({ type: "error", seq: 0, session_id: "sess-1", code: "machine_not_found", message: "x" });
    expect(state(a).phase).toBe("paused");
    startDownload(b);
    await flush();
    expect(state(b).phase).toBe("downloading");
    expect(requests(b)).toEqual([0]);
  });

  it("a stall pause with the socket up hands the slot to what is queued", async () => {
    const a = file(1, CHUNK);
    const b = file(2, CHUNK);
    startDownload(a);
    startDownload(b);
    await flush();
    expect(state(b).phase).toBe("queued");
    await vi.advanceTimersByTimeAsync(STALL_MS * (MAX_STRIKES + 1));
    expect(state(a)).toMatchObject({ phase: "paused", waiting: true });
    expect(state(b).phase).toBe("downloading");
    await serve(b);
    expect(state(b).phase).toBe("done");
  });

  it("Pause hands the slot on, and Resume waits its turn", async () => {
    const a = file(1, CHUNK);
    const b = file(2, CHUNK);
    startDownload(a);
    startDownload(b);
    await flush();
    pauseDownload(a);
    await flush();
    expect(state(b).phase).toBe("downloading");
    await resumeDownload(a);
    expect(state(a).phase).toBe("queued");
    await serve(b);
    expect(state(a).phase).toBe("downloading");
    await serve(a);
    expect(state(a).phase).toBe("done");
  });

  it("a download resumed from the queue drops its old pause note", async () => {
    const a = file(1, CHUNK);
    const b = file(2, CHUNK, { sessionId: "sess-2" });
    startDownload(a);
    await flush();
    emit({ type: "error", seq: 0, session_id: "sess-1", code: "machine_not_found", message: "x" });
    expect(state(a).note).toBe("studio-mac is not reachable.");
    startDownload(b);
    await flush();
    await resumeDownload(a);
    await serve(b);
    await serve(a);
    expect(state(a)).toMatchObject({ phase: "done", note: null, code: null });
  });

  it("a dropped connection keeps the slot, so the reconnect carries on with it first", async () => {
    const a = file(1, CHUNK);
    const b = file(2, CHUNK);
    startDownload(a);
    startDownload(b);
    await flush();
    for (const handler of bus.states) handler("reconnecting");
    await flush();
    expect([state(a).phase, state(b).phase]).toEqual(["paused", "queued"]);
    for (const handler of bus.states) handler("connected");
    await flush();
    expect([state(a).phase, state(b).phase]).toEqual(["downloading", "queued"]);
  });
});

describe("keep-awake and what comes after", () => {
  it("holds the phone awake while bytes move and lets go after", async () => {
    const ref = file(1, CHUNK);
    startDownload(ref);
    await flush();
    expect(host.keepAwake.mock.calls).toEqual([[true]]);
    await serve(ref);
    expect(host.keepAwake.mock.calls).toEqual([[true], [false]]);

    const next = file(2);
    startDownload(next);
    await flush();
    pauseDownload(next);
    expect(host.keepAwake.mock.calls).toEqual([[true], [false], [true], [false]]);
  });

  it("Download and install opens the APK, unless its line has a warning", async () => {
    const apk = file(1, CHUNK, { name: "app-release.apk", mime: "application/vnd.android.package-archive" });
    await runAction(apk, "download-install");
    await flush();
    await serve(apk);
    await flush();
    expect(host.open).toHaveBeenCalledTimes(1);

    const risky = file(2, CHUNK, { name: "other.apk", mime: "application/vnd.android.package-archive" });
    sinkOf(risky).statusOverride = {
      state: "verified",
      have: CHUNK,
      canInstall: true,
      apk: {
        package: "com.thatsimpletech.tstdesk",
        versionCode: 10,
        versionName: "0.1.9",
        isSelf: true,
        installedVersionCode: 9,
        installedVersionName: "0.1.8",
        sameSigner: false,
      },
    };
    sinkOf(risky).beginState = "verified";
    await runAction(risky, "download-install");
    await flush();
    expect(state(risky).phase).toBe("done");
    expect(host.open).toHaveBeenCalledTimes(1);
  });

  it("Save to Downloads before the file is in downloads it first", async () => {
    const ref = file(1, CHUNK);
    await runAction(ref, "save-downloads");
    await flush();
    await serve(ref);
    await flush();
    expect(host.saveToDownloads).toHaveBeenCalledTimes(1);
    expect(state(ref)).toMatchObject({ phase: "saved", savedTo: "Download/TST Desk/file-1.bin" });
  });

  it("Cancel throws the part away", async () => {
    const ref = file(1);
    startDownload(ref);
    await flush();
    await reply(ref, 0);
    await runAction(ref, "cancel");
    await flush();
    expect(sinkOf(ref).discards).toBe(1);
    expect(state(ref)).toMatchObject({ phase: "idle", have: 0 });
  });
});
