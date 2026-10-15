// The pull engine (TD-5144, design §5.2), against a fake sink, a fake socket
// and fake timers: the window, ordering, resume, stalls, the connection,
// the codes that land on the card, the queue and keep-awake.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ArtifactChunk, ClientMessageUnion, DaemonEventUnion } from "./protocol";

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

vi.mock("./machines.svelte.js", () => ({ machineNameFor: () => "studio-mac" }));

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
import { fileKey, type SentFileRef } from "./sent-files";

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
    expect(state(ref).phase).toBe("unavailable");
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
