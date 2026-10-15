// Where a downloaded sent file goes, per platform (TD-5144, design §6.2).
//
// The pull engine (downloads.svelte.ts) asks the daemon for chunks and hands
// each one, in order, to a sink. A sink keeps the part on disk (or in
// memory), checks the sha256 at the end and puts the finished file where
// the user can open it. Three sinks:
//
// - Android 0.1.9: the `window.TstDeskFiles` bridge (§1.7). Every call but
//   two carries a key the phone hands only to the top frame, because the
//   bridge is injected into iframes too. Results of the async calls come
//   back on `__tstDeskOnFile`, routed here by id.
// - Desktop: the shell's `received_file_*` commands (§1.6). Ids only, never
//   a path; appends are serialized so two never race on the `.part` file.
// - Browser: memory, up to 256 MiB, verified with WebCrypto and saved with
//   a Blob and `<a download>`.
//
// A host adds the per-platform actions on a file a card offers (open, show
// in folder, save a copy, save to Downloads, keep the phone awake).

import { codeOf, type ApkStatus, type Platform, type SentFileRef } from "./sent-files";

/** What a sink needs to know about the file before the first byte. */
export interface FileMeta {
  id: string;
  sessionId: string;
  name: string;
  size: number;
  mime: string;
  sha256: string;
}

export type BeginState = "none" | "partial" | "verified";
export type SinkState = BeginState | "local" | "received";

/** status(): the phone adds its APK verdict once the file is verified. */
export interface SinkStatus {
  state: SinkState;
  have: number;
  apk?: ApkStatus | null;
  canInstall?: boolean;
}

export type FinishResult = { ok: true; localPath?: string } | { ok: false; code: string };

export interface FileSink {
  begin(meta: FileMeta): Promise<{ have: number; state: BeginState }>;
  /** Resolves to the new contiguous length. */
  append(offset: number, dataB64: string): Promise<number>;
  finish(): Promise<FinishResult>;
  status(): Promise<SinkStatus>;
  discard(): Promise<void>;
}

/** A refusal from a sink or host. `have` is set when the sink says where
 *  the part really ends (`bad_offset`). */
export class SinkError extends Error {
  readonly code: string;
  readonly have: number | null;
  constructor(code: string, have: number | null = null) {
    super(code);
    this.code = code;
    this.have = have;
  }
}

/** The actions a card's buttons run, on top of the sink. */
export interface FileHost {
  platform: Platform;
  sink(ref: SentFileRef): FileSink;
  open(ref: SentFileRef): Promise<void>;
  reveal(ref: SentFileRef): Promise<void>;
  /** The path saved to, or null when the user cancelled the dialog. */
  saveAs(ref: SentFileRef): Promise<string | null>;
  /** Where the phone saved it ("Download/TST Desk/<name>"). */
  saveToDownloads(ref: SentFileRef): Promise<string>;
  remove(ref: SentFileRef): Promise<void>;
  keepAwake(on: boolean): void;
}

export function metaFor(ref: SentFileRef): FileMeta {
  return {
    id: ref.artifactId,
    sessionId: ref.sessionId,
    name: ref.name,
    size: ref.size,
    mime: ref.mime,
    sha256: ref.sha256,
  };
}

// ── Android: window.TstDeskFiles (§1.7) ────────────────────────────────

/** The bridge exactly as the phone exposes it: strings in, JSON strings out. */
export interface TstDeskFilesBridge {
  filesVersion(): number;
  filesKeyRequest(): string;
  fileBegin(key: string, metaJson: string): string;
  fileAppend(key: string, id: string, offset: string, b64: string): string;
  fileFinish(key: string, id: string): string;
  fileStatus(key: string, id: string): string;
  fileOpen(key: string, id: string): string;
  fileSaveToDownloads(key: string, id: string): string;
  fileDiscard(key: string, id: string): string;
  fileKeepAwake(key: string, on: string): string;
}

/** One `__tstDeskOnFile` event. */
export interface FileEvent {
  event: string;
  id: string;
  code?: string;
  where?: string;
}

interface FilesGlobals {
  TstDeskFiles?: TstDeskFilesBridge;
  __tstDeskOnFile?: (raw: unknown) => void;
  __tstDeskOnFilesKey?: (raw: unknown) => void;
}

/** How long to wait for the key to come back on the top frame. */
export const KEY_TIMEOUT_MS = 5000;
/** While the phone hashes, look at status this often: an event sent while
 *  the page was hidden is dropped (§1.7), and the file may be done anyway. */
export const FINISH_POLL_MS = 5000;

type Reply = Record<string, unknown>;

function parseReply(raw: unknown): Reply {
  if (typeof raw !== "string") return { ok: false, code: "io" };
  try {
    const out: unknown = JSON.parse(raw);
    return out !== null && typeof out === "object" ? (out as Reply) : { ok: false, code: "io" };
  } catch {
    return { ok: false, code: "io" };
  }
}

function replyCode(reply: Reply): string {
  return typeof reply.code === "string" ? reply.code : "io";
}

function replyHave(reply: Reply): number | null {
  return typeof reply.have === "number" ? reply.have : null;
}

function parseEvent(raw: unknown): FileEvent | null {
  let value = raw;
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
  if (value === null || typeof value !== "object") return null;
  const e = value as Record<string, unknown>;
  if (typeof e.event !== "string" || typeof e.id !== "string") return null;
  return {
    event: e.event,
    id: e.id,
    code: typeof e.code === "string" ? e.code : undefined,
    where: typeof e.where === "string" ? e.where : undefined,
  };
}

/** The key handshake and the event routing for one page. */
class AndroidFiles {
  private key: string | null = null;
  private pendingKey: Promise<string> | null = null;
  private resolveKey: ((key: string) => void) | null = null;
  private readonly listeners = new Map<string, Set<(event: FileEvent) => void>>();
  private readonly onKey: (raw: unknown) => void;
  private readonly onFile: (raw: unknown) => void;

  constructor(
    readonly bridge: TstDeskFilesBridge,
    private readonly scope: FilesGlobals,
  ) {
    this.onKey = (raw) => {
      if (typeof raw !== "string" || !/^[0-9a-f]{16,}$/.test(raw)) return;
      this.key = raw;
      this.resolveKey?.(raw);
      this.resolveKey = null;
      this.pendingKey = null;
    };
    this.onFile = (raw) => {
      const event = parseEvent(raw);
      if (event === null) return;
      for (const listener of [...(this.listeners.get(event.id) ?? [])]) listener(event);
    };
    // Installed before the first key request: the phone only delivers to a
    // page that already has the callback.
    scope.__tstDeskOnFilesKey = this.onKey;
    scope.__tstDeskOnFile = this.onFile;
  }

  uninstall(): void {
    if (this.scope.__tstDeskOnFilesKey === this.onKey) delete this.scope.__tstDeskOnFilesKey;
    if (this.scope.__tstDeskOnFile === this.onFile) delete this.scope.__tstDeskOnFile;
  }

  private requestKey(): Promise<string> {
    if (this.pendingKey !== null) return this.pendingKey;
    this.key = null;
    const pending = new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pendingKey !== pending) return;
        this.pendingKey = null;
        this.resolveKey = null;
        reject(new SinkError("bad_key"));
      }, KEY_TIMEOUT_MS);
      this.resolveKey = (key) => {
        clearTimeout(timer);
        resolve(key);
      };
    });
    this.pendingKey = pending;
    const asked = parseReply(safeCall(() => this.bridge.filesKeyRequest()));
    if (asked.ok !== true) {
      this.pendingKey = null;
      this.resolveKey = null;
      return Promise.reject(new SinkError("bad_key"));
    }
    return pending;
  }

  /** Run one keyed call. A `bad_key` reply (an iframe asked for a key and
   *  rotated ours) asks for a fresh key and tries again, once. */
  async call(run: (key: string) => string): Promise<Reply> {
    const key = this.key ?? (await this.requestKey());
    const first = parseReply(safeCall(() => run(key)));
    if (first.ok !== false || first.code !== "bad_key") return first;
    const fresh = await this.requestKey();
    const second = parseReply(safeCall(() => run(fresh)));
    if (second.ok === false && second.code === "bad_key") throw new SinkError("bad_key");
    return second;
  }

  /** Wait for the next event for `id` whose name is in `names`. Registered
   *  before the call that causes it, so a fast reply is never missed. */
  waitFor(id: string, names: readonly string[]): { promise: Promise<FileEvent>; cancel: () => void; settle: (e: FileEvent) => void } {
    let done = false;
    let resolveEvent: (event: FileEvent) => void = () => {};
    const promise = new Promise<FileEvent>((resolve) => {
      resolveEvent = resolve;
    });
    const set = this.listeners.get(id) ?? new Set<(event: FileEvent) => void>();
    this.listeners.set(id, set);
    const cancel = (): void => {
      done = true;
      set.delete(listener);
      if (set.size === 0) this.listeners.delete(id);
    };
    const settle = (event: FileEvent): void => {
      if (done) return;
      cancel();
      resolveEvent(event);
    };
    const listener = (event: FileEvent): void => {
      if (names.includes(event.event)) settle(event);
    };
    set.add(listener);
    return { promise, cancel, settle };
  }
}

function safeCall(run: () => string): unknown {
  try {
    return run();
  } catch {
    return null;
  }
}

const androidByScope = new WeakMap<object, AndroidFiles>();

function androidFiles(scope: object): AndroidFiles {
  const g = scope as FilesGlobals;
  const bridge = g.TstDeskFiles;
  if (bridge === undefined) throw new SinkError("android_old");
  const existing = androidByScope.get(scope);
  if (existing !== undefined && existing.bridge === bridge) return existing;
  existing?.uninstall();
  const files = new AndroidFiles(bridge, g);
  androidByScope.set(scope, files);
  return files;
}

/** Take the page callbacks back off the page (tests, teardown). */
export function uninstallFileCallbacks(scope: object = globalThis): void {
  androidByScope.get(scope)?.uninstall();
  androidByScope.delete(scope);
}

function asStatus(reply: Reply): SinkStatus {
  if (reply.ok === false) throw new SinkError(replyCode(reply), replyHave(reply));
  const state = reply.state === "verified" || reply.state === "partial" ? reply.state : "none";
  return {
    state,
    have: typeof reply.have === "number" ? reply.have : 0,
    apk: (reply.apk as ApkStatus | null | undefined) ?? null,
    canInstall: reply.canInstall === true,
  };
}

export function createAndroidSink(ref: SentFileRef, scope: object = globalThis): FileSink {
  const files = androidFiles(scope);
  const id = ref.artifactId;
  const sink: FileSink = {
    async begin(meta) {
      const reply = await files.call((key) => files.bridge.fileBegin(key, JSON.stringify(meta)));
      if (reply.ok !== true) throw new SinkError(replyCode(reply));
      const state = reply.state === "verified" || reply.state === "partial" ? reply.state : "none";
      return { have: typeof reply.have === "number" ? reply.have : 0, state };
    },
    async append(offset, dataB64) {
      const reply = await files.call((key) => files.bridge.fileAppend(key, id, String(offset), dataB64));
      if (reply.ok !== true) throw new SinkError(replyCode(reply), replyHave(reply));
      return typeof reply.have === "number" ? reply.have : offset;
    },
    async finish() {
      const wait = files.waitFor(id, ["verified", "failed"]);
      const poll = setInterval(() => {
        void sink.status().then(
          (s) => {
            if (s.state === "verified") wait.settle({ event: "verified", id });
          },
          () => {},
        );
      }, FINISH_POLL_MS);
      try {
        const started = await files.call((key) => files.bridge.fileFinish(key, id));
        if (started.ok !== true) {
          wait.cancel();
          return { ok: false, code: replyCode(started) };
        }
        const event = await wait.promise;
        return event.event === "verified" ? { ok: true } : { ok: false, code: event.code ?? "io" };
      } catch (err) {
        wait.cancel();
        return { ok: false, code: codeOf(err) };
      } finally {
        clearInterval(poll);
      }
    },
    async status() {
      return asStatus(await files.call((key) => files.bridge.fileStatus(key, id)));
    },
    async discard() {
      await files.call((key) => files.bridge.fileDiscard(key, id));
    },
  };
  return sink;
}

/** An async bridge call: `started` now, then one event of `ok` or `fail`. */
async function androidAsync(
  files: AndroidFiles,
  id: string,
  run: (key: string) => string,
  ok: string,
  fail: string,
): Promise<FileEvent> {
  const wait = files.waitFor(id, [ok, fail]);
  try {
    const started = await files.call(run);
    if (started.ok !== true) throw new SinkError(replyCode(started));
  } catch (err) {
    wait.cancel();
    throw err instanceof SinkError ? err : new SinkError(codeOf(err));
  }
  const event = await wait.promise;
  if (event.event === fail) throw new SinkError(event.code ?? "io");
  return event;
}

export function createAndroidHost(scope: object = globalThis): FileHost {
  const files = (): AndroidFiles => androidFiles(scope);
  return {
    platform: "android",
    sink: (ref) => createAndroidSink(ref, scope),
    async open(ref) {
      const f = files();
      await androidAsync(f, ref.artifactId, (key) => f.bridge.fileOpen(key, ref.artifactId), "opened", "open_failed");
    },
    async reveal() {
      throw new SinkError("no_app");
    },
    async saveAs() {
      throw new SinkError("no_app");
    },
    async saveToDownloads(ref) {
      const f = files();
      const event = await androidAsync(
        f,
        ref.artifactId,
        (key) => f.bridge.fileSaveToDownloads(key, ref.artifactId),
        "saved",
        "save_failed",
      );
      return event.where ?? `Download/TST Desk/${ref.name}`;
    },
    async remove(ref) {
      const f = files();
      await f.call((key) => f.bridge.fileDiscard(key, ref.artifactId));
    },
    keepAwake(on) {
      const f = files();
      void f.call((key) => f.bridge.fileKeepAwake(key, on ? "1" : "0")).catch(() => {});
    },
  };
}

// ── Desktop: the shell's commands (§1.6) ───────────────────────────────

/** `FileStatus` from the shell. */
interface ShellStatus {
  state: "local" | "received" | "partial" | "none";
  have: number;
}

export type Invoke = <T>(cmd: string, args: Record<string, unknown>) => Promise<T>;

const tauriInvoke: Invoke = async <T>(cmd: string, args: Record<string, unknown>): Promise<T> => {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(cmd, args);
};

/** Shell errors are strings: `bad_offset:<have>`, `io: <detail>`, ... */
function shellError(err: unknown): SinkError {
  const code = codeOf(err);
  if (code === "bad_offset" && typeof err === "string") {
    const have = Number(err.slice(err.indexOf(":") + 1).trim());
    return new SinkError(code, Number.isSafeInteger(have) && have >= 0 ? have : null);
  }
  return new SinkError(code);
}

async function shell<T>(invoke: Invoke, cmd: string, args: Record<string, unknown>): Promise<T> {
  try {
    return await invoke<T>(cmd, args);
  } catch (err) {
    throw shellError(err);
  }
}

export function createTauriSink(ref: SentFileRef, invoke: Invoke = tauriInvoke): FileSink {
  const artifactId = ref.artifactId;
  let chain: Promise<unknown> = Promise.resolve();
  return {
    async begin(meta) {
      const status = await shell<ShellStatus>(invoke, "received_file_begin", {
        artifactId,
        sessionId: meta.sessionId,
        name: meta.name,
        size: meta.size,
        sha256: meta.sha256,
      });
      if (status.state === "received" || status.state === "local") return { have: meta.size, state: "verified" };
      return { have: status.have, state: status.have > 0 ? "partial" : "none" };
    },
    append(offset, dataB64) {
      // One write at a time, in call order: the shell refuses an offset
      // that is not the current end of the part.
      const next = chain.then(() => shell<number>(invoke, "received_file_append", { artifactId, offset, dataB64 }));
      chain = next.catch(() => {});
      return next;
    },
    async finish() {
      try {
        const localPath = await shell<string>(invoke, "received_file_finish", { artifactId });
        return { ok: true, localPath };
      } catch (err) {
        return { ok: false, code: codeOf(err) };
      }
    },
    async status() {
      const status = await shell<ShellStatus>(invoke, "sent_file_status", { sessionId: ref.sessionId, artifactId });
      return { state: status.state, have: status.have };
    },
    async discard() {
      await shell<null>(invoke, "received_file_discard", { artifactId });
    },
  };
}

export function createTauriHost(invoke: Invoke = tauriInvoke): FileHost {
  const ids = (ref: SentFileRef) => ({ sessionId: ref.sessionId, artifactId: ref.artifactId });
  return {
    platform: "desktop",
    sink: (ref) => createTauriSink(ref, invoke),
    async open(ref) {
      await shell<null>(invoke, "sent_file_open", ids(ref));
    },
    async reveal(ref) {
      await shell<null>(invoke, "sent_file_reveal", ids(ref));
    },
    async saveAs(ref) {
      return (await shell<string | null>(invoke, "sent_file_save_as", { ...ids(ref), name: ref.name })) ?? null;
    },
    async saveToDownloads() {
      throw new SinkError("no_app");
    },
    async remove(ref) {
      await shell<null>(invoke, "received_file_discard", { artifactId: ref.artifactId });
    },
    keepAwake() {
      // The desktop does not sleep mid-download on its own account.
    },
  };
}

// ── Browser: memory, WebCrypto, <a download> ───────────────────────────

/** A browser tab holds the whole file in memory, so it stops here. */
export const BROWSER_MAX_BYTES = 256 * 1024 * 1024;

interface MemoryPart {
  meta: FileMeta;
  parts: Uint8Array[];
  have: number;
}

const memory = new Map<string, MemoryPart>();

export type SaveBlob = (blob: Blob, name: string) => void;

function saveWithAnchor(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // The download has its own handle on the blob by now; a minute is slack.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function decodeB64(data: string): Uint8Array {
  const raw = atob(data);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function createBrowserSink(ref: SentFileRef, save: SaveBlob = saveWithAnchor): FileSink {
  const id = ref.artifactId;
  return {
    async begin(meta) {
      if (meta.size > BROWSER_MAX_BYTES) throw new SinkError("too_large");
      const held = memory.get(id);
      if (held !== undefined && held.meta.size === meta.size && held.meta.sha256 === meta.sha256) {
        return { have: held.have, state: held.have > 0 ? "partial" : "none" };
      }
      memory.set(id, { meta, parts: [], have: 0 });
      return { have: 0, state: "none" };
    },
    async append(offset, dataB64) {
      const held = memory.get(id);
      if (held === undefined) throw new SinkError("io");
      if (offset !== held.have) throw new SinkError("bad_offset", held.have);
      let bytes: Uint8Array;
      try {
        bytes = decodeB64(dataB64);
      } catch {
        throw new SinkError("bad_data", held.have);
      }
      if (held.have + bytes.length > held.meta.size) throw new SinkError("bad_data", held.have);
      held.parts.push(bytes);
      held.have += bytes.length;
      return held.have;
    },
    async finish() {
      const held = memory.get(id);
      if (held === undefined) return { ok: false, code: "io" };
      if (held.have !== held.meta.size) return { ok: false, code: "size_mismatch" };
      const whole = new Uint8Array(held.have);
      let at = 0;
      for (const part of held.parts) {
        whole.set(part, at);
        at += part.length;
      }
      if ((await sha256Hex(whole)) !== held.meta.sha256) {
        memory.delete(id);
        return { ok: false, code: "hash_mismatch" };
      }
      memory.delete(id);
      save(new Blob([whole as BlobPart], { type: held.meta.mime || "application/octet-stream" }), held.meta.name);
      return { ok: true };
    },
    async status() {
      const held = memory.get(id);
      return held === undefined ? { state: "none", have: 0 } : { state: held.have > 0 ? "partial" : "none", have: held.have };
    },
    async discard() {
      memory.delete(id);
    },
  };
}

export function createBrowserHost(save: SaveBlob = saveWithAnchor): FileHost {
  const unsupported = async (): Promise<never> => {
    throw new SinkError("no_app");
  };
  return {
    platform: "browser",
    sink: (ref) => createBrowserSink(ref, save),
    open: unsupported,
    reveal: unsupported,
    saveAs: unsupported,
    saveToDownloads: unsupported,
    async remove(ref) {
      memory.delete(ref.artifactId);
    },
    keepAwake() {},
  };
}

/** Drop every in-memory part (tests). */
export function resetBrowserParts(): void {
  memory.clear();
}

/** The host for this page's platform. An APK before 0.1.9 has no sink. */
export function createFileHost(platform: Platform, scope: object = globalThis): FileHost | null {
  switch (platform) {
    case "android":
      return createAndroidHost(scope);
    case "desktop":
      return createTauriHost();
    case "browser":
      return createBrowserHost();
    case "android-old":
      return null;
  }
}
