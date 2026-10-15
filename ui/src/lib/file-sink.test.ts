// The three file sinks (TD-5144, design §6.2): the phone's TstDeskFiles
// bridge with the exact JSON of §1.7, the desktop shell's commands with
// camelCase args (§1.6), and the browser's memory sink.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const tauri = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: tauri.invoke }));

import {
  BROWSER_MAX_BYTES,
  createAndroidHost,
  createAndroidSink,
  createBrowserSink,
  createFileHost,
  createTauriHost,
  createTauriSink,
  KEY_TIMEOUT_MS,
  metaFor,
  resetBrowserParts,
  SinkError,
  uninstallFileCallbacks,
  type TstDeskFilesBridge,
} from "./file-sink";
import { detectPlatform, type SentFileRef } from "./sent-files";

const HI_SHA = "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4";

function ref(over: Partial<SentFileRef> = {}): SentFileRef {
  return {
    artifactId: "a".repeat(32),
    sessionId: "sess-1",
    name: "app-release.apk",
    size: 2,
    mime: "application/vnd.android.package-archive",
    sha256: HI_SHA,
    caption: null,
    toolCallId: "call_abc",
    ...over,
  };
}

// ── A fake phone, answering exactly as §1.7 says ───────────────────────

interface Page {
  TstDeskFiles?: TstDeskFilesBridge;
  __tstDeskOnFile?: (raw: unknown) => void;
  __tstDeskOnFilesKey?: (raw: unknown) => void;
}

class FakePhone implements TstDeskFilesBridge {
  key = "";
  keyRequests = 0;
  /** What fileFinish/fileOpen/fileSaveToDownloads report later. */
  outcome: Record<string, Record<string, unknown>> = {};
  calls: Array<[string, ...string[]]> = [];
  /** Off: async calls start but the test sends the events itself. */
  autoEvents = true;
  status: Record<string, unknown> = { state: "none", have: 0, size: 2, canInstall: false, apk: null };
  private n = 0;

  constructor(private readonly page: Page) {}

  /** The phone only delivers to a page that already has the callback. */
  private callPage(name: "__tstDeskOnFile" | "__tstDeskOnFilesKey", value: unknown): void {
    queueMicrotask(() => this.page[name]?.(value));
  }

  /** An iframe asked for a key, so ours is stale now. */
  rotate(): void {
    this.n += 1;
    this.key = (this.n.toString(16) + "f".repeat(32)).slice(0, 32);
  }

  private guard(key: string): string | null {
    return key === this.key && key !== "" ? null : JSON.stringify({ ok: false, code: "bad_key" });
  }

  private async(key: string, id: string, method: string): string {
    const bad = this.guard(key);
    if (bad !== null) return bad;
    const event = this.outcome[method] ?? { event: "failed", code: "io" };
    if (this.autoEvents) this.callPage("__tstDeskOnFile", { id, ...event });
    return JSON.stringify({ ok: true, started: true });
  }

  filesVersion(): number {
    return 1;
  }
  filesKeyRequest(): string {
    this.keyRequests += 1;
    this.rotate();
    this.callPage("__tstDeskOnFilesKey", this.key);
    return JSON.stringify({ ok: true });
  }
  fileBegin(key: string, metaJson: string): string {
    this.calls.push(["fileBegin", key, metaJson]);
    return this.guard(key) ?? JSON.stringify({ ok: true, have: 0, state: "none" });
  }
  fileAppend(key: string, id: string, offset: string, b64: string): string {
    this.calls.push(["fileAppend", key, id, offset, b64]);
    const bad = this.guard(key);
    if (bad !== null) return bad;
    if (offset !== "0") return JSON.stringify({ ok: false, code: "bad_offset", have: 0 });
    return JSON.stringify({ ok: true, have: 2 });
  }
  fileFinish(key: string, id: string): string {
    this.calls.push(["fileFinish", key, id]);
    return this.async(key, id, "fileFinish");
  }
  fileStatus(key: string, id: string): string {
    this.calls.push(["fileStatus", key, id]);
    return this.guard(key) ?? JSON.stringify(this.status);
  }
  fileOpen(key: string, id: string): string {
    this.calls.push(["fileOpen", key, id]);
    return this.async(key, id, "fileOpen");
  }
  fileSaveToDownloads(key: string, id: string): string {
    this.calls.push(["fileSaveToDownloads", key, id]);
    return this.async(key, id, "fileSaveToDownloads");
  }
  fileDiscard(key: string, id: string): string {
    this.calls.push(["fileDiscard", key, id]);
    return this.guard(key) ?? JSON.stringify({ ok: true });
  }
  fileKeepAwake(key: string, on: string): string {
    this.calls.push(["fileKeepAwake", key, on]);
    return this.guard(key) ?? JSON.stringify({ ok: true });
  }
}

describe("Android sink (window.TstDeskFiles, §1.7)", () => {
  let page: Page;
  let phone: FakePhone;

  beforeEach(() => {
    page = {};
    phone = new FakePhone(page);
    page.TstDeskFiles = phone;
  });

  afterEach(() => {
    uninstallFileCallbacks(page);
    vi.useRealTimers();
  });

  it("installs both callbacks before asking for the key, then calls with it", async () => {
    let installedFirst = false;
    const ask = phone.filesKeyRequest.bind(phone);
    phone.filesKeyRequest = () => {
      installedFirst = typeof page.__tstDeskOnFilesKey === "function" && typeof page.__tstDeskOnFile === "function";
      return ask();
    };
    const sink = createAndroidSink(ref(), page);
    await expect(sink.begin(metaFor(ref()))).resolves.toEqual({ have: 0, state: "none" });

    expect(installedFirst).toBe(true);
    expect(phone.keyRequests).toBe(1);
    const [, key, metaJson] = phone.calls[0];
    expect(key).toBe(phone.key);
    expect(key).toMatch(/^[0-9a-f]{32}$/);
    expect(JSON.parse(metaJson)).toEqual({
      id: "a".repeat(32),
      sessionId: "sess-1",
      name: "app-release.apk",
      size: 2,
      mime: "application/vnd.android.package-archive",
      sha256: HI_SHA,
    });
  });

  it("passes the offset as a decimal string and reads have back", async () => {
    const sink = createAndroidSink(ref(), page);
    await expect(sink.append(0, "aGk=")).resolves.toBe(2);
    expect(phone.calls.at(-1)).toEqual(["fileAppend", phone.key, "a".repeat(32), "0", "aGk="]);
    await expect(sink.append(2, "aGk=")).rejects.toMatchObject({ code: "bad_offset", have: 0 });
  });

  it("re-requests the key once on bad_key, then gives up", async () => {
    const sink = createAndroidSink(ref(), page);
    await sink.begin(metaFor(ref()));
    phone.rotate();
    await expect(sink.append(0, "aGk=")).resolves.toBe(2);
    expect(phone.keyRequests).toBe(2);

    // A key that goes stale again at once: one retry, then bad_key.
    const append = phone.fileAppend.bind(phone);
    phone.fileAppend = (_key, id, offset, b64) => append("stale", id, offset, b64);
    await expect(sink.append(0, "aGk=")).rejects.toMatchObject({ code: "bad_key" });
    expect(phone.keyRequests).toBe(3);
  });

  it("gives bad_key when the key never arrives", async () => {
    vi.useFakeTimers();
    phone.filesKeyRequest = () => JSON.stringify({ ok: true });
    const sink = createAndroidSink(ref(), page);
    const begin = sink.begin(metaFor(ref()));
    const caught = begin.catch((err: unknown) => err);
    await vi.advanceTimersByTimeAsync(KEY_TIMEOUT_MS);
    expect(await caught).toBeInstanceOf(SinkError);
    expect(await caught).toMatchObject({ code: "bad_key" });
  });

  it("routes finish events by id", async () => {
    const one = ref({ artifactId: "1".repeat(32) });
    const two = ref({ artifactId: "2".repeat(32) });
    phone.autoEvents = false;
    const finishA = createAndroidSink(one, page).finish();
    const finishB = createAndroidSink(two, page).finish();
    await vi.waitFor(() => expect(phone.calls.filter((c) => c[0] === "fileFinish")).toHaveLength(2));

    // Crossed, and one for a file nobody is waiting on: each lands on its own id.
    page.__tstDeskOnFile?.({ event: "verified", id: "9".repeat(32) });
    page.__tstDeskOnFile?.({ event: "verified", id: two.artifactId });
    page.__tstDeskOnFile?.({ event: "failed", id: one.artifactId, code: "hash_mismatch" });
    await expect(finishB).resolves.toEqual({ ok: true });
    await expect(finishA).resolves.toEqual({ ok: false, code: "hash_mismatch" });
  });

  it("an async call on a bad id starts, then fails on the event (the phone's way)", async () => {
    phone.outcome.fileFinish = { event: "failed", code: "io" };
    await expect(createAndroidSink(ref({ artifactId: "b".repeat(32) }), page).finish()).resolves.toEqual({
      ok: false,
      code: "io",
    });
  });

  it("reads fileStatus, APK verdict included", async () => {
    const apk = {
      package: "com.example.foo",
      versionCode: 3,
      versionName: "1.2",
      isSelf: false,
      installedVersionCode: null,
      installedVersionName: null,
      sameSigner: null,
    };
    phone.status = { state: "verified", have: 2, size: 2, canInstall: true, apk };
    const sink = createAndroidSink(ref(), page);
    await expect(sink.status()).resolves.toEqual({ state: "verified", have: 2, apk, canInstall: true });
  });

  it("opens and saves through the async events, and keeps the phone awake", async () => {
    const host = createAndroidHost(page);
    phone.outcome.fileOpen = { event: "opened" };
    await expect(host.open(ref())).resolves.toBeUndefined();
    phone.outcome.fileOpen = { event: "open_failed", code: "no_app" };
    await expect(host.open(ref())).rejects.toMatchObject({ code: "no_app" });

    phone.outcome.fileSaveToDownloads = { event: "saved", where: "Download/TST Desk/app-release.apk" };
    await expect(host.saveToDownloads(ref())).resolves.toBe("Download/TST Desk/app-release.apk");
    phone.outcome.fileSaveToDownloads = { event: "save_failed", code: "needs_android_10" };
    await expect(host.saveToDownloads(ref())).rejects.toMatchObject({ code: "needs_android_10" });

    host.keepAwake(true);
    host.keepAwake(false);
    await vi.waitFor(() =>
      expect(phone.calls.filter((c) => c[0] === "fileKeepAwake").map((c) => c[2])).toEqual(["1", "0"]),
    );
    await host.remove(ref());
    expect(phone.calls.at(-1)).toEqual(["fileDiscard", phone.key, "a".repeat(32)]);
  });

  it("has no sink without TstDeskFiles: that phone is android-old", () => {
    const old = { TstDeskNative: {} };
    expect(detectPlatform(old)).toBe("android-old");
    expect(createFileHost("android-old", old)).toBeNull();
    expect(() => createAndroidSink(ref(), old)).toThrow(SinkError);
  });
});

// ── Desktop: the shell's commands ──────────────────────────────────────

describe("Tauri sink (§1.6)", () => {
  beforeEach(() => {
    tauri.invoke.mockReset();
  });

  it("calls received_file_* and sent_file_status with camelCase ids, never a path", async () => {
    tauri.invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "received_file_begin") return { state: "partial", have: 1 };
      if (cmd === "received_file_append") return 2;
      if (cmd === "received_file_finish") return "/home/u/Downloads/app-release.apk";
      if (cmd === "sent_file_status") return { state: "received", have: 2 };
      return null;
    });
    const r = ref();
    const sink = createTauriSink(r);
    await expect(sink.begin(metaFor(r))).resolves.toEqual({ have: 1, state: "partial" });
    await expect(sink.append(1, "aQ==")).resolves.toBe(2);
    await expect(sink.finish()).resolves.toEqual({ ok: true, localPath: "/home/u/Downloads/app-release.apk" });
    await expect(sink.status()).resolves.toEqual({ state: "received", have: 2 });
    await sink.discard();

    const host = createTauriHost();
    await host.open(r);
    await host.reveal(r);
    await host.saveAs(r);

    const id = "a".repeat(32);
    expect(tauri.invoke.mock.calls).toEqual([
      ["received_file_begin", { artifactId: id, sessionId: "sess-1", name: "app-release.apk", size: 2, sha256: HI_SHA }],
      ["received_file_append", { artifactId: id, offset: 1, dataB64: "aQ==" }],
      ["received_file_finish", { artifactId: id }],
      ["sent_file_status", { sessionId: "sess-1", artifactId: id }],
      ["received_file_discard", { artifactId: id }],
      ["sent_file_open", { sessionId: "sess-1", artifactId: id }],
      ["sent_file_reveal", { sessionId: "sess-1", artifactId: id }],
      ["sent_file_save_as", { sessionId: "sess-1", artifactId: id, name: "app-release.apk" }],
    ]);
  });

  it("reads a received or local file as verified", async () => {
    tauri.invoke.mockResolvedValueOnce({ state: "received", have: 2 });
    await expect(createTauriSink(ref()).begin(metaFor(ref()))).resolves.toEqual({ have: 2, state: "verified" });
    tauri.invoke.mockResolvedValueOnce({ state: "none", have: 0 });
    await expect(createTauriSink(ref()).begin(metaFor(ref()))).resolves.toEqual({ have: 0, state: "none" });
  });

  it("parses the shell's error strings", async () => {
    tauri.invoke.mockRejectedValueOnce("bad_offset:1048576");
    await expect(createTauriSink(ref()).append(0, "aGk=")).rejects.toMatchObject({ code: "bad_offset", have: 1048576 });
    tauri.invoke.mockRejectedValueOnce("no_app: xdg-open exited 3");
    await expect(createTauriHost().open(ref())).rejects.toMatchObject({ code: "no_app" });
    tauri.invoke.mockRejectedValueOnce("hash_mismatch");
    await expect(createTauriSink(ref()).finish()).resolves.toEqual({ ok: false, code: "hash_mismatch" });
  });

  it("writes one append at a time, in call order", async () => {
    const order: string[] = [];
    let release: () => void = () => {};
    tauri.invoke.mockImplementation(async (_cmd: string, args: { offset: number }) => {
      order.push(`start ${args.offset}`);
      if (args.offset === 0) await new Promise<void>((resolve) => (release = resolve));
      order.push(`end ${args.offset}`);
      return args.offset + 1;
    });
    const sink = createTauriSink(ref());
    const first = sink.append(0, "aA==");
    const second = sink.append(1, "aQ==");
    await vi.waitFor(() => expect(order).toEqual(["start 0"]));
    release();
    await expect(Promise.all([first, second])).resolves.toEqual([1, 2]);
    expect(order).toEqual(["start 0", "end 0", "start 1", "end 1"]);
  });
});

// ── Browser: memory and WebCrypto ──────────────────────────────────────

describe("Browser sink", () => {
  beforeEach(() => {
    resetBrowserParts();
  });

  it("verifies the sha256 and saves the whole file once", async () => {
    const save = vi.fn();
    const sink = createBrowserSink(ref(), save);
    await sink.begin(metaFor(ref()));
    await expect(sink.append(0, "aGk=")).resolves.toBe(2);
    await expect(sink.finish()).resolves.toEqual({ ok: true });
    expect(save).toHaveBeenCalledTimes(1);
    const [blob, name] = save.mock.calls[0] as [Blob, string];
    expect(name).toBe("app-release.apk");
    expect(await blob.text()).toBe("hi");
  });

  it("drops a damaged download and never saves it", async () => {
    const save = vi.fn();
    const r = ref({ sha256: "0".repeat(64) });
    const sink = createBrowserSink(r, save);
    await sink.begin(metaFor(r));
    await sink.append(0, "aGk=");
    await expect(sink.finish()).resolves.toEqual({ ok: false, code: "hash_mismatch" });
    expect(save).not.toHaveBeenCalled();
    await expect(sink.status()).resolves.toEqual({ state: "none", have: 0 });
  });

  it("stops at 256 MiB", async () => {
    const big = ref({ size: BROWSER_MAX_BYTES + 1 });
    await expect(createBrowserSink(big, vi.fn()).begin(metaFor(big))).rejects.toMatchObject({ code: "too_large" });
    const cap = ref({ size: BROWSER_MAX_BYTES });
    await expect(createBrowserSink(cap, vi.fn()).begin(metaFor(cap))).resolves.toEqual({ have: 0, state: "none" });
  });

  it("refuses a gap, bad base64 and bytes past the size", async () => {
    const sink = createBrowserSink(ref(), vi.fn());
    await sink.begin(metaFor(ref()));
    await expect(sink.append(1, "aGk=")).rejects.toMatchObject({ code: "bad_offset", have: 0 });
    await expect(sink.append(0, "@@")).rejects.toMatchObject({ code: "bad_data" });
    await expect(sink.append(0, "aGkh")).rejects.toMatchObject({ code: "bad_data", have: 0 });
    await sink.append(0, "aA==");
    await expect(sink.status()).resolves.toEqual({ state: "partial", have: 1 });
    await expect(sink.finish()).resolves.toEqual({ ok: false, code: "size_mismatch" });
  });
});
