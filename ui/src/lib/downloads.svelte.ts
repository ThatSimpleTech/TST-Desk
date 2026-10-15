// The pull engine for files the agent sent (TD-5144, design §5.2).
//
// A sent file's bytes ride the WebSocket the page is already authenticated
// on: the client asks for `read_artifact_chunk` at an offset and the daemon
// (or, for another computer's session, the hub on its behalf) answers with
// `artifact_chunk`. This store keeps a window of three 512 KiB requests in
// flight, puts replies back in order, hands them to the platform's sink
// (file-sink.ts), and verifies the whole file at the end.
//
// One download runs at a time; the rest queue, so two APKs never double the
// socket load. A request with no reply for 20 s is asked again; three of
// those and the card pauses. A dropped connection pauses too, and the
// download picks up where the sink's part ends once the socket is back.
// Every card's state lives in `downloads`, keyed `${sessionId}|${artifactId}`,
// and the first render probes the sink so a card comes back in its real
// state after a restart (§6.3).

import { onConnectionState, onDaemonEvent, onResume, sendToDaemon } from "./connection-status.svelte.js";
import { claimInline } from "./inline-errors";
import { machineNameFor } from "./machines.svelte.js";
import type { ArtifactChunk, ClientMessageUnion, DaemonEventUnion } from "./protocol";
import { createFileHost, metaFor, type FileHost, type FileSink, type SinkStatus } from "./file-sink";
import {
  apkWarnings,
  codeOf,
  detectPlatform,
  fileKey,
  fileKind,
  messageFor,
  THIS_COMPUTER,
  type ApkStatus,
  type FileAction,
  type FilePhase,
  type Platform,
  type SentFileRef,
} from "./sent-files";

export const CHUNK = 524288;
export const WINDOW = 3;
export const TICK_MS = 5000;
export const STALL_MS = 20000;
export const MAX_STRIKES = 3;
/** A sink that keeps answering bad_offset without moving is broken. */
const MAX_RESYNCS = 3;

/** One card's state. Plain data, so a card can render it as is. */
export interface DownloadState {
  phase: FilePhase;
  /** Bytes the sink holds, contiguous from 0. */
  have: number;
  /** Bytes per second, smoothed. Zero until the second append. */
  rate: number;
  /** Mapped copy (§6.5) when the phase's own line is not the whole story. */
  note: string | null;
  code: string | null;
  /** A pause that waits for the connection, not for Resume. */
  waiting: boolean;
  /** The desktop holds the daemon's own copy (a session on this computer). */
  local: boolean;
  /** The phone holds part of the file. */
  partial: boolean;
  savedTo: string | null;
  apk: ApkStatus | null;
  canInstall: boolean;
  /** no_app on the desktop: offer Show in folder with the copy. */
  reveal: boolean;
}

export const downloads: Record<string, DownloadState> = $state({});

/** What the first render shows until the probe answers. */
const CHECKING: DownloadState = Object.freeze({
  phase: "checking",
  have: 0,
  rate: 0,
  note: null,
  code: null,
  waiting: false,
  local: false,
  partial: false,
  savedTo: null,
  apk: null,
  canInstall: false,
  reveal: false,
}) as DownloadState;

type Then = "open" | "install" | "save" | null;

interface Inflight {
  at: number;
  strikes: number;
}

interface Job {
  ref: SentFileRef;
  key: string;
  sink: FileSink;
  then: Then;
  inflight: Map<number, Inflight>;
  /** Replies that arrived ahead of `have`, by offset. */
  reorder: Map<number, string>;
  /** The next offset not yet asked for. */
  next: number;
  have: number;
  hashRetried: boolean;
  rangeRetried: boolean;
  resyncs: number;
  appending: boolean;
  lastAppendAt: number;
  /** Bumped on pause, cancel and restart, so a stale await lands nowhere. */
  generation: number;
}

interface Deps {
  platform: Platform;
  host: FileHost | null;
  send: (msg: ClientMessageUnion) => boolean;
  /** The `<machine>` in the copy. */
  machine: (sessionId: string) => string;
}

let deps: Deps | null = null;
const jobs = new Map<string, Job>();
const queue: string[] = [];
const probing = new Set<string>();
let active: string | null = null;
let ticker: ReturnType<typeof setInterval> | null = null;
let awake = false;
let stops: Array<() => void> = [];

function getDeps(): Deps {
  if (deps === null) {
    const platform = detectPlatform();
    deps = {
      platform,
      host: createFileHost(platform),
      send: sendToDaemon,
      machine: (sessionId) => machineNameFor(sessionId) ?? THIS_COMPUTER,
    };
  }
  return deps;
}

function ensureStarted(): void {
  if (stops.length > 0) return;
  stops = [onDaemonEvent(handleEvent), onConnectionState(handleState), onResume(handleResume)];
}

/** Swap the platform, host or transport (tests; a page with a fixed host). */
export function configureDownloads(overrides: Partial<Deps>): void {
  deps = { ...getDeps(), ...overrides };
}

/** Forget every card, job and subscription (tests, teardown). */
export function resetDownloads(): void {
  for (const stop of stops) stop();
  stops = [];
  for (const job of jobs.values()) job.generation += 1;
  jobs.clear();
  queue.length = 0;
  probing.clear();
  active = null;
  if (ticker !== null) clearInterval(ticker);
  ticker = null;
  awake = false;
  deps = null;
  for (const key of Object.keys(downloads)) delete downloads[key];
}

/** The platform this page downloads on. */
export function downloadPlatform(): Platform {
  return getDeps().platform;
}

function entry(ref: SentFileRef): DownloadState {
  const key = fileKey(ref);
  const existing = downloads[key];
  if (existing !== undefined) return existing;
  downloads[key] = { ...CHECKING, phase: "idle" };
  return downloads[key];
}

/** A card's state. The first read probes the sink, off the render, so a
 *  card after a restart shows what is really on disk (§6.3). */
export function stateFor(ref: SentFileRef): DownloadState {
  const key = fileKey(ref);
  const existing = downloads[key];
  if (existing !== undefined) return existing;
  if (!probing.has(key)) {
    probing.add(key);
    queueMicrotask(() => void probe(ref));
  }
  return CHECKING;
}

async function probe(ref: SentFileRef): Promise<void> {
  const key = fileKey(ref);
  const d = getDeps();
  let status: SinkStatus = { state: "none", have: 0 };
  if (d.platform !== "android-old" && d.host !== null) {
    try {
      status = await d.host.sink(ref).status();
    } catch {
      // Nothing to read: offer the download.
    }
  }
  probing.delete(key);
  // A tap that started a download while the probe ran wins.
  if (downloads[key] !== undefined) return;
  const e = entry(ref);
  if (d.platform === "android-old") {
    e.phase = "android-old";
    return;
  }
  applyStatus(e, status, ref);
}

function applyStatus(e: DownloadState, status: SinkStatus, ref: SentFileRef): void {
  e.apk = status.apk ?? null;
  e.canInstall = status.canInstall ?? false;
  switch (status.state) {
    case "local":
      e.phase = "local";
      e.local = true;
      e.have = ref.size;
      return;
    case "received":
    case "verified":
      e.phase = "done";
      e.have = ref.size;
      e.partial = false;
      return;
    case "partial":
      e.phase = "paused";
      e.have = status.have;
      e.partial = true;
      e.waiting = false;
      return;
    case "none":
      e.phase = "idle";
      e.have = 0;
      e.partial = false;
      return;
  }
}

// ── Starting, queueing, finishing ─────────────────────────────────────

function newJob(ref: SentFileRef, sink: FileSink, then: Then): Job {
  return {
    ref,
    key: fileKey(ref),
    sink,
    then,
    inflight: new Map(),
    reorder: new Map(),
    next: 0,
    have: 0,
    hashRetried: false,
    rangeRetried: false,
    resyncs: 0,
    appending: false,
    lastAppendAt: 0,
    generation: 0,
  };
}

function isCurrent(job: Job, generation: number): boolean {
  return jobs.get(job.key) === job && job.generation === generation;
}

function isRunning(job: Job): boolean {
  return jobs.get(job.key) === job && downloads[job.key]?.phase === "downloading";
}

/** Download a file, then open, install or save it when `then` says so. */
export function startDownload(ref: SentFileRef, then: Then = null): void {
  ensureStarted();
  const key = fileKey(ref);
  const existing = jobs.get(key);
  if (existing !== undefined) {
    if (downloads[key]?.phase === "paused") void resumeDownload(ref);
    return;
  }
  const host = getDeps().host;
  if (host === null) return;
  const job = newJob(ref, host.sink(ref), then);
  jobs.set(key, job);
  const e = entry(ref);
  e.note = null;
  e.code = null;
  e.reveal = false;
  e.savedTo = null;
  if (active === null) {
    active = key;
    void begin(job);
  } else {
    queue.push(key);
    e.phase = "queued";
  }
}

async function begin(job: Job): Promise<void> {
  const generation = job.generation;
  const e = entry(job.ref);
  e.phase = "downloading";
  e.rate = 0;
  e.waiting = false;
  syncAwake();
  let begun: { have: number; state: "none" | "partial" | "verified" };
  try {
    begun = await job.sink.begin(metaFor(job.ref));
  } catch (err) {
    if (isCurrent(job, generation)) handleCode(job, codeOf(err));
    return;
  }
  if (!isCurrent(job, generation)) return;
  if (begun.state === "verified") {
    await complete(job);
    return;
  }
  startAt(job, begun.have);
}

/** Pull from `have`: finish at once when the sink already holds it all. */
function startAt(job: Job, have: number): void {
  job.have = have;
  job.next = have;
  job.lastAppendAt = Date.now();
  job.inflight.clear();
  job.reorder.clear();
  entry(job.ref).have = have;
  if (have >= job.ref.size) {
    void finish(job);
    return;
  }
  fill(job);
}

function request(job: Job, offset: number): boolean {
  const sent = getDeps().send({
    type: "read_artifact_chunk",
    session_id: job.ref.sessionId,
    artifact_id: job.ref.artifactId,
    offset,
    length: CHUNK,
  });
  if (!sent) {
    pauseJob(job, true);
    return false;
  }
  return true;
}

/** Keep WINDOW requests out, counting replies waiting their turn. */
function fill(job: Job): void {
  while (isRunning(job) && job.next < job.ref.size && job.inflight.size + job.reorder.size < WINDOW) {
    const offset = job.next;
    job.next += CHUNK;
    job.inflight.set(offset, { at: Date.now(), strikes: 0 });
    if (!request(job, offset)) return;
  }
  ensureTicker();
}

function onChunk(event: ArtifactChunk): void {
  const key = fileKey({ sessionId: event.session_id, artifactId: event.artifact_id });
  const job = jobs.get(key);
  if (job === undefined || !isRunning(job)) return;
  // Only replies to what is out now; a late one from before a pause or a
  // restart is not ours any more.
  if (!job.inflight.has(event.offset)) return;
  job.inflight.delete(event.offset);
  if (event.error_code !== null && event.error_code !== "") {
    handleCode(job, event.error_code);
    return;
  }
  if (event.size !== job.ref.size || event.sha256 !== job.ref.sha256) {
    handleCode(job, "changed");
    return;
  }
  if (event.offset < job.have) return;
  job.reorder.set(event.offset, event.data_b64);
  void drain(job);
}

/** Append every reply that is next in line, one at a time. */
async function drain(job: Job): Promise<void> {
  if (job.appending) return;
  job.appending = true;
  const generation = job.generation;
  try {
    while (isRunning(job) && job.reorder.has(job.have)) {
      const offset = job.have;
      const data = job.reorder.get(offset) ?? "";
      job.reorder.delete(offset);
      let next: number;
      try {
        next = await job.sink.append(offset, data);
      } catch (err) {
        if (!isCurrent(job, generation)) return;
        const code = codeOf(err);
        if (code !== "bad_offset") {
          handleCode(job, code);
          return;
        }
        const have = (err as { have?: unknown }).have;
        await resync(job, typeof have === "number" ? have : null);
        return;
      }
      if (!isCurrent(job, generation)) return;
      noteRate(job, next - offset);
      job.resyncs = next > offset ? 0 : job.resyncs;
      job.have = next;
      entry(job.ref).have = next;
      fill(job);
    }
  } finally {
    job.appending = false;
  }
  if (isCurrent(job, generation) && isRunning(job) && job.have >= job.ref.size) await finish(job);
}

/** `bad_offset`: carry on from where the sink says the part ends. */
async function resync(job: Job, have: number | null): Promise<void> {
  job.resyncs += 1;
  if (job.resyncs > MAX_RESYNCS) {
    handleCode(job, "io");
    return;
  }
  let at = have;
  if (at === null) {
    const generation = job.generation;
    try {
      at = (await job.sink.status()).have;
    } catch (err) {
      if (isCurrent(job, generation)) handleCode(job, codeOf(err));
      return;
    }
    if (!isCurrent(job, generation)) return;
  }
  job.generation += 1;
  startAt(job, at);
}

function noteRate(job: Job, bytes: number): void {
  const now = Date.now();
  const seconds = (now - job.lastAppendAt) / 1000;
  job.lastAppendAt = now;
  if (seconds <= 0 || bytes <= 0) return;
  const instant = bytes / seconds;
  const e = entry(job.ref);
  e.rate = e.rate > 0 ? 0.3 * instant + 0.7 * e.rate : instant;
}

async function finish(job: Job): Promise<void> {
  const generation = job.generation;
  const e = entry(job.ref);
  e.phase = "verifying";
  syncAwake();
  let result: Awaited<ReturnType<FileSink["finish"]>>;
  try {
    result = await job.sink.finish();
  } catch (err) {
    result = { ok: false, code: codeOf(err) };
  }
  if (!isCurrent(job, generation)) return;
  if (result.ok) {
    await complete(job);
    return;
  }
  if (result.code === "hash_mismatch" && !job.hashRetried) {
    // Damaged on the way: start over from nothing, once.
    job.hashRetried = true;
    try {
      await job.sink.discard();
    } catch {
      // begin() starts a fresh part either way.
    }
    if (!isCurrent(job, generation)) return;
    job.generation += 1;
    e.have = 0;
    void begin(job);
    return;
  }
  handleCode(job, result.code);
}

async function complete(job: Job): Promise<void> {
  const generation = job.generation;
  let status: SinkStatus | null = null;
  try {
    status = await job.sink.status();
  } catch {
    // The APK line waits for the next render's probe.
  }
  if (!isCurrent(job, generation)) return;
  const e = entry(job.ref);
  e.phase = status?.state === "local" ? "local" : "done";
  e.have = job.ref.size;
  e.partial = false;
  e.waiting = false;
  e.apk = status?.apk ?? null;
  e.canInstall = status?.canInstall ?? false;
  release(job);
  const then = job.then;
  if (then === "save") {
    void runAction(job.ref, "save-downloads");
  } else if (then === "open" || then === "install") {
    // A warning on the APK line stops the automatic install (§6.4).
    const warned = fileKind(job.ref.name, job.ref.mime) === "apk" && e.apk !== null && apkWarnings(e.apk).length > 0;
    if (!warned) void runAction(job.ref, "open");
  }
}

/** Give the slot to the next queued download. */
function release(job: Job): void {
  if (jobs.get(job.key) === job) jobs.delete(job.key);
  job.generation += 1;
  const at = queue.indexOf(job.key);
  if (at !== -1) queue.splice(at, 1);
  if (active === job.key) {
    active = null;
    startNext();
  }
  syncAwake();
}

function startNext(): void {
  while (active === null && queue.length > 0) {
    const key = queue.shift() as string;
    const job = jobs.get(key);
    if (job === undefined) continue;
    active = key;
    void begin(job);
  }
}

/** Land a code on the card (§6.5). */
function handleCode(job: Job, code: string): void {
  const outcome = messageFor(code, copyContext(job.ref));
  const e = entry(job.ref);
  e.code = code;
  switch (outcome.kind) {
    case "restart":
      if (job.rangeRetried) {
        stopWith(job, "failed", `Download failed (${code}).`);
        return;
      }
      job.rangeRetried = true;
      void restartFromStatus(job);
      return;
    case "resync":
      void resync(job, null);
      return;
    case "rekey":
      // The sink already asked for a fresh key once.
      stopWith(job, "failed", outcome.copy);
      return;
    case "phase":
      if (outcome.phase === "paused") {
        pauseJob(job, false, outcome.copy);
        return;
      }
      stopWith(job, outcome.phase === "done" ? "failed" : outcome.phase, outcome.copy);
      return;
  }
}

function stopWith(job: Job, phase: FilePhase, copy: string): void {
  const e = entry(job.ref);
  e.phase = phase;
  e.note = copy;
  e.waiting = false;
  release(job);
}

async function restartFromStatus(job: Job): Promise<void> {
  job.generation += 1;
  const generation = job.generation;
  job.inflight.clear();
  job.reorder.clear();
  let status: SinkStatus;
  try {
    status = await job.sink.status();
  } catch (err) {
    if (isCurrent(job, generation)) handleCode(job, codeOf(err));
    return;
  }
  if (!isCurrent(job, generation)) return;
  if (status.state === "verified" || status.state === "received") {
    await complete(job);
    return;
  }
  startAt(job, status.have);
}

function copyContext(ref: SentFileRef) {
  const d = getDeps();
  return { machine: d.machine(ref.sessionId), platform: d.platform, name: ref.name, size: ref.size };
}

// ── Pause, resume, cancel ──────────────────────────────────────────────

/** Stop asking, keep the part. The job keeps its slot, so a reconnect
 *  carries on with it before anything queued. */
function pauseJob(job: Job, waiting: boolean, note: string | null = null): void {
  job.generation += 1;
  job.inflight.clear();
  job.reorder.clear();
  job.next = job.have;
  const e = entry(job.ref);
  e.phase = "paused";
  e.waiting = waiting;
  e.note = note;
  e.rate = 0;
  e.partial = job.have > 0;
  syncAwake();
}

export function pauseDownload(ref: SentFileRef): void {
  const job = jobs.get(fileKey(ref));
  if (job !== undefined && isRunning(job)) pauseJob(job, false);
}

/** Carry on from the sink's own count. A part found after a reload has no
 *  job yet: that starts one, and begin() resumes from the part. */
export async function resumeDownload(ref: SentFileRef): Promise<void> {
  ensureStarted();
  const key = fileKey(ref);
  const job = jobs.get(key);
  if (job === undefined) {
    startDownload(ref, null);
    return;
  }
  const e = entry(ref);
  if (e.phase !== "paused") return;
  if (active !== null && active !== key) {
    if (!queue.includes(key)) queue.push(key);
    e.phase = "queued";
    return;
  }
  active = key;
  job.generation += 1;
  const generation = job.generation;
  e.phase = "downloading";
  e.waiting = false;
  e.note = null;
  syncAwake();
  let status: SinkStatus;
  try {
    status = await job.sink.status();
  } catch (err) {
    if (isCurrent(job, generation)) handleCode(job, codeOf(err));
    return;
  }
  if (!isCurrent(job, generation)) return;
  if (status.state === "verified" || status.state === "received" || status.state === "local") {
    await complete(job);
    return;
  }
  startAt(job, status.have);
}

/** Stop and throw the part away. */
export function cancelDownload(ref: SentFileRef): void {
  const key = fileKey(ref);
  const job = jobs.get(key);
  const e = entry(ref);
  if (job !== undefined) {
    release(job);
    void job.sink.discard().catch(() => {});
  } else {
    void getDeps().host?.sink(ref).discard().catch(() => {});
  }
  e.phase = "idle";
  e.have = 0;
  e.rate = 0;
  e.partial = false;
  e.waiting = false;
  e.note = null;
  e.code = null;
}

// ── Ticks, the connection, resume ──────────────────────────────────────

function ensureTicker(): void {
  if (ticker !== null) return;
  ticker = setInterval(tick, TICK_MS);
}

function tick(): void {
  const now = Date.now();
  let running = false;
  for (const job of [...jobs.values()]) {
    if (!isRunning(job)) continue;
    running = true;
    for (const [offset, info] of [...job.inflight]) {
      if (now - info.at < STALL_MS) continue;
      if (info.strikes >= MAX_STRIKES) {
        pauseJob(job, true);
        break;
      }
      // Reads are idempotent over an immutable copy: just ask again.
      info.strikes += 1;
      info.at = now;
      if (!request(job, offset)) break;
    }
  }
  if (!running && ticker !== null) {
    clearInterval(ticker);
    ticker = null;
  }
}

/** Resume what a dropped connection or a stall paused in this page's life. */
function resumeWaiting(): void {
  for (const job of [...jobs.values()]) {
    const e = downloads[job.key];
    if (e?.phase === "paused" && e.waiting) void resumeDownload(job.ref);
  }
}

function handleState(state: string): void {
  if (state === "connected") {
    resumeWaiting();
    return;
  }
  for (const job of [...jobs.values()]) {
    if (isRunning(job)) pauseJob(job, true);
  }
}

function handleResume(): void {
  resumeWaiting();
}

function handleEvent(event: DaemonEventUnion): void {
  if (event.type === "artifact_chunk") {
    onChunk(event);
    return;
  }
  if (event.type === "error") claimDownloadError(event);
}

/** Errors that answer a chunk request rather than anything else (§5.2):
 *  claimed here, before the notice sink, and shown on the card instead. */
export function claimDownloadError(event: DaemonEventUnion): boolean {
  if (event.type !== "error") return false;
  const job = active === null ? undefined : jobs.get(active);
  if (job === undefined) return false;
  const phase = downloads[job.key]?.phase;
  if (phase !== "downloading" && phase !== "paused") return false;
  const viaHub =
    (event.code === "not_on_remote" || event.code === "not_allowed" || event.code === "machine_not_found") &&
    event.session_id === job.ref.sessionId;
  const oldPeer = event.code === "unknown_message" && event.message.includes("read_artifact_chunk");
  if (!viaHub && !oldPeer) return false;
  claimInline(event);
  handleCode(job, event.code);
  return true;
}

/** Keep the phone awake while bytes move or the file is being checked. */
function syncAwake(): void {
  let want = false;
  for (const job of jobs.values()) {
    const phase = downloads[job.key]?.phase;
    if (phase === "downloading" || phase === "verifying") want = true;
  }
  if (want === awake) return;
  awake = want;
  getDeps().host?.keepAwake(want);
}

// ── Card actions ───────────────────────────────────────────────────────

/** Run one of a card's buttons. */
export async function runAction(ref: SentFileRef, action: FileAction): Promise<void> {
  ensureStarted();
  const host = getDeps().host;
  if (host === null) return;
  const e = entry(ref);
  switch (action) {
    case "download":
    case "retry":
      startDownload(ref, null);
      return;
    case "download-open":
      startDownload(ref, "open");
      return;
    case "download-install":
      startDownload(ref, "install");
      return;
    case "resume":
      await resumeDownload(ref);
      return;
    case "cancel":
      cancelDownload(ref);
      return;
    case "save-downloads":
      if (e.phase !== "done" && e.phase !== "saved") {
        startDownload(ref, "save");
        return;
      }
      try {
        const where = await host.saveToDownloads(ref);
        e.phase = "saved";
        e.savedTo = where;
        e.note = null;
      } catch (err) {
        actionFailed(ref, codeOf(err));
      }
      return;
    case "open":
    case "install":
    case "install-anyway":
      try {
        await host.open(ref);
        e.note = null;
        e.reveal = false;
      } catch (err) {
        actionFailed(ref, codeOf(err));
      }
      return;
    case "reveal":
      try {
        await host.reveal(ref);
      } catch (err) {
        actionFailed(ref, codeOf(err));
      }
      return;
    case "save-as":
      try {
        const where = await host.saveAs(ref);
        if (where !== null) {
          e.phase = "saved";
          e.savedTo = where;
          e.note = null;
        }
      } catch (err) {
        actionFailed(ref, codeOf(err));
      }
      return;
    case "remove":
      try {
        await host.remove(ref);
      } catch {
        // Gone already is what was asked for.
      }
      e.phase = "idle";
      e.have = 0;
      e.partial = false;
      e.apk = null;
      e.savedTo = null;
      e.note = null;
      return;
  }
}

/** An open, reveal or save that failed. The file is still there, so the
 *  card keeps its phase and says why, unless the file is not (§6.5). */
function actionFailed(ref: SentFileRef, code: string): void {
  const e = entry(ref);
  const outcome = messageFor(code, copyContext(ref));
  e.code = code;
  if (outcome.kind !== "phase") {
    e.note = `Download failed (${code}).`;
    return;
  }
  if (outcome.phase === "gone" || code === "not_verified") {
    e.phase = outcome.phase;
    e.note = outcome.copy;
    return;
  }
  e.note = outcome.copy;
  e.reveal = outcome.reveal === true;
}
