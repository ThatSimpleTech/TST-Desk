// Files the agent sent to the chat (TD-5144): the pure half.
//
// The daemon keeps a copy of each sent file and announces it with an
// `artifact_ready` whose kind is "sent_file" (TD-5143). This module turns
// that event into a card's data, decides which buttons a card offers on
// each platform and phase, and owns every line of copy a card can show,
// including one line for every error code the daemon, the desktop shell and
// the phone can hand back (design §1.8, §6.5). No stores, no DOM: the pull
// engine (downloads.svelte.ts) and the cards share these rules.

import { formatBytes } from "./attachments";
import type { ArtifactEntry, ArtifactReady } from "./protocol";

/** One file the agent sent, as a card needs it. */
export interface SentFileRef {
  artifactId: string;
  sessionId: string;
  name: string;
  size: number;
  mime: string;
  sha256: string;
  caption: string | null;
  toolCallId: string | null;
}

/** The id grammar every layer checks first (`uuid4().hex`). */
export const ARTIFACT_ID_RE = /^[0-9a-f]{32}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;

/** The card's data, or null unless the event is a well-formed sent file. A
 *  TD-3201 artifact (kind absent or "artifact") is the Artifacts pane's. */
export function sentFileFromEvent(e: ArtifactReady | ArtifactEntry, sessionId: string): SentFileRef | null {
  if (e.kind !== "sent_file") return null;
  if (!ARTIFACT_ID_RE.test(e.artifact_id)) return null;
  if (typeof e.name !== "string" || e.name === "") return null;
  if (typeof e.size !== "number" || !Number.isSafeInteger(e.size) || e.size < 0) return null;
  if (typeof e.sha256 !== "string" || !SHA256_RE.test(e.sha256)) return null;
  return {
    artifactId: e.artifact_id,
    sessionId,
    name: e.name,
    size: e.size,
    mime: e.mime,
    sha256: e.sha256,
    caption: typeof e.caption === "string" && e.caption !== "" ? e.caption : null,
    toolCallId: e.tool_call_id ?? null,
  };
}

/** The key the download store files a card under. */
export function fileKey(ref: Pick<SentFileRef, "sessionId" | "artifactId">): string {
  return `${ref.sessionId}|${ref.artifactId}`;
}

// ── Kinds ──────────────────────────────────────────────────────────────

export type FileKind = "apk" | "image" | "pdf" | "archive" | "video" | "audio" | "text" | "program" | "other";

/** Things that run when opened, for the kind's label and icon. What may be
 *  opened on a computer is the allowlist below, not this list. */
const PROGRAM_EXTS = new Set([
  ".exe", ".msi", ".bat", ".cmd", ".com", ".ps1", ".sh", ".command", ".app",
  ".appimage", ".desktop", ".jar", ".deb", ".rpm", ".pkg", ".dmg", ".run",
]);
const ARCHIVE_EXTS = new Set([".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"]);
const ARCHIVE_MIMES = new Set([
  "application/zip", "application/x-tar", "application/gzip", "application/x-7z-compressed",
]);

/** The last extension, lowercased and dot included, or "" (§1.4: a dot at
 *  index 0 is a hidden file, not an extension). */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "";
  return name.slice(dot).toLowerCase();
}

/** The only names a computer opens with one click (amended §6.2): inert
 *  documents, pictures, sound, video and archives. A denylist missed
 *  `.js .vbs .hta .scr .lnk .url`, which Windows runs on Open. Everything else
 *  gets Show in folder and Save a copy; the shell refuses the rest too, with
 *  `not_openable`. The phone keeps Open for every kind. */
export const DESKTOP_OPEN_EXTS: ReadonlySet<string> = new Set([
  ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".mp4", ".webm", ".mov", ".mkv", ".mp3", ".wav",
  ".m4a", ".ogg", ".flac", ".aac", ".txt", ".md", ".csv", ".log", ".json", ".zip", ".tar", ".gz", ".tgz", ".7z",
]);

/** The name without its trailing spaces and dots, as every computer saves
 *  it (§1.4 step 4): "x.exe " lands on disk as x.exe. */
export function savedNameTail(name: string): string {
  return name.replace(/[ .]+$/, "");
}

/** Whether the desktop may offer Open for this name. */
export function desktopOpenable(name: string): boolean {
  return DESKTOP_OPEN_EXTS.has(extensionOf(savedNameTail(name)));
}

export function fileKind(name: string, mime: string): FileKind {
  const ext = extensionOf(savedNameTail(name));
  const m = mime.toLowerCase();
  if (ext === ".apk" || m === "application/vnd.android.package-archive") return "apk";
  if (PROGRAM_EXTS.has(ext)) return "program";
  if (m.startsWith("image/")) return "image";
  if (m === "application/pdf" || ext === ".pdf") return "pdf";
  if (ARCHIVE_EXTS.has(ext) || ARCHIVE_MIMES.has(m)) return "archive";
  if (m.startsWith("video/")) return "video";
  if (m.startsWith("audio/")) return "audio";
  if (m.startsWith("text/") || m === "application/json") return "text";
  return "other";
}

const KIND_LABELS: Record<FileKind, string> = {
  apk: "Android app",
  image: "Image",
  pdf: "PDF",
  archive: "Archive",
  video: "Video",
  audio: "Audio",
  text: "Text",
  program: "Program",
  other: "File",
};

export function kindLabel(kind: FileKind): string {
  return KIND_LABELS[kind];
}

/** Line 2 at rest: "Android app · 46.2 MB". */
export function sizeLine(ref: Pick<SentFileRef, "name" | "mime" | "size">): string {
  return `${kindLabel(fileKind(ref.name, ref.mime))} · ${formatBytes(ref.size)}`;
}

/** "File: app-release.apk, 46.2 MB" for the card's group label. */
export function cardLabel(ref: Pick<SentFileRef, "name" | "size">): string {
  return `File: ${ref.name}, ${formatBytes(ref.size)}`;
}

/** The skeleton shown while a send_file call runs: the basename of the
 *  first path, and how many more ride with it. */
export function sendingText(files: unknown): string | null {
  if (!Array.isArray(files) || files.length === 0 || typeof files[0] !== "string") return null;
  const first = files[0].replace(/\\/g, "/").split("/").pop() ?? "";
  if (first === "") return null;
  const more = files.length - 1;
  return more > 0 ? `Sending ${first} and ${more} more…` : `Sending ${first}…`;
}

/** Split a name so the middle can ellipsize and the extension stays on
 *  screen: the head truncates, the tail never does. Code points, so a
 *  surrogate pair is never cut in half. */
export function nameParts(name: string): { head: string; tail: string } {
  const chars = Array.from(name);
  const keep = extensionOf(name).length + 6;
  if (chars.length <= keep + 4) return { head: name, tail: "" };
  return { head: chars.slice(0, -keep).join(""), tail: chars.slice(-keep).join("") };
}

// ── Platforms ──────────────────────────────────────────────────────────

export type Platform = "android" | "android-old" | "desktop" | "browser";

interface FilesBridgeProbe {
  TstDeskFiles?: { filesVersion?: () => number };
  TstDeskNative?: unknown;
}

/** Where this page runs. An APK before 0.1.9 bundles its own older UI, so
 *  `android-old` is defensive only. */
export function detectPlatform(scope: object = globalThis): Platform {
  const g = scope as FilesBridgeProbe;
  const files = g.TstDeskFiles;
  if (files !== undefined && files !== null) {
    try {
      if (typeof files.filesVersion === "function" && files.filesVersion() >= 1) return "android";
    } catch {
      // A throwing bridge is no bridge.
    }
    return "android-old";
  }
  if (g.TstDeskNative !== undefined && g.TstDeskNative !== null) return "android-old";
  if ("__TAURI_INTERNALS__" in scope) return "desktop";
  return "browser";
}

// ── Phases and actions (§6.4) ──────────────────────────────────────────

/** A card's phase. `checking` is the moment before the first-render probe
 *  answers (§6.3): the size line shows and no button does yet. */
export type FilePhase =
  | "sending"
  | "checking"
  | "idle"
  | "local"
  | "queued"
  | "downloading"
  | "paused"
  | "verifying"
  | "done"
  | "saved"
  | "failed"
  | "unavailable"
  | "gone"
  | "android-old";

export type FileAction =
  | "download"
  | "download-install"
  | "download-open"
  | "save-downloads"
  | "open"
  | "reveal"
  | "save-as"
  | "install"
  | "install-anyway"
  | "remove"
  | "cancel"
  | "resume"
  | "retry";

export interface ActionSet {
  primary: FileAction | null;
  menu: FileAction[];
}

/** What else decides a card's buttons. `local`: the desktop has its own
 *  copy (a session on this computer). `warning`: the APK line carries a
 *  warning, so Install reads Install anyway. `partial`: the phone holds a
 *  part of the file. */
export interface ActionContext {
  local?: boolean;
  warning?: boolean;
  partial?: boolean;
  /** Desktop no_app: Show in folder becomes the button (§6.5). */
  reveal?: boolean;
  /** The name is on the desktop's Open allowlist (`desktopOpenable`).
   *  Unset means no: a caller that forgets it never gets Open. */
  openable?: boolean;
}

const NONE: ActionSet = { primary: null, menu: [] };

/** The action matrix of §6.4, per platform × phase × kind. */
export function fileActions(platform: Platform, kind: FileKind, phase: FilePhase, ctx: ActionContext = {}): ActionSet {
  switch (phase) {
    case "sending":
    case "checking":
    case "verifying":
    case "unavailable":
    case "android-old":
      return NONE;
    case "idle":
      if (platform === "android") {
        return { primary: kind === "apk" ? "download-install" : "download-open", menu: ["save-downloads"] };
      }
      if (platform === "desktop" || platform === "browser") return { primary: "download", menu: [] };
      return NONE;
    case "local":
      if (platform !== "desktop") return NONE;
      if (!desktopOpens(kind, ctx)) return { primary: "reveal", menu: ["save-as"] };
      return { primary: "open", menu: ["reveal", "save-as"] };
    case "queued":
    case "downloading":
      return platform === "android-old" ? NONE : { primary: "cancel", menu: [] };
    case "paused":
      if (platform === "browser") return { primary: "resume", menu: [] };
      if (platform === "desktop" || platform === "android") return { primary: "resume", menu: ["cancel"] };
      return NONE;
    case "done":
      return doneActions(platform, kind, ctx);
    case "saved":
      if (platform === "android") return { primary: "open", menu: [] };
      if (platform === "desktop") return ctx.local === true ? fileActions(platform, kind, "local", ctx) : doneActions(platform, kind, ctx);
      return NONE;
    case "failed":
      return platform === "android-old" ? NONE : { primary: "retry", menu: [] };
    case "gone":
      return platform === "android" && ctx.partial === true ? { primary: null, menu: ["remove"] } : NONE;
  }
}

/** Open on the desktop: an allowlisted name that no_app has not ruled out. */
function desktopOpens(kind: FileKind, ctx: ActionContext): boolean {
  return ctx.openable === true && kind !== "program" && ctx.reveal !== true;
}

function doneActions(platform: Platform, kind: FileKind, ctx: ActionContext): ActionSet {
  if (platform === "desktop") {
    return desktopOpens(kind, ctx) ? { primary: "open", menu: ["reveal"] } : { primary: "reveal", menu: [] };
  }
  if (platform === "android") {
    const primary: FileAction = kind === "apk" ? (ctx.warning === true ? "install-anyway" : "install") : "open";
    return { primary, menu: ["save-downloads", "remove"] };
  }
  return NONE;
}

const ACTION_LABELS: Record<FileAction, string> = {
  download: "Download",
  "download-install": "Download and install",
  "download-open": "Download and open",
  "save-downloads": "Save to Downloads",
  open: "Open",
  reveal: "Show in folder",
  "save-as": "Save a copy…",
  install: "Install",
  "install-anyway": "Install anyway",
  remove: "Remove from phone",
  cancel: "Cancel",
  resume: "Resume",
  retry: "Retry",
};

export function actionLabel(action: FileAction): string {
  return ACTION_LABELS[action];
}

// ── Progress copy ──────────────────────────────────────────────────────

/** "12.4 of 46.2 MB · 8 s left". The rate is bytes per second; zero or
 *  unknown leaves the time out rather than guessing. */
export function progressText(have: number, size: number, rate: number): string {
  const total = formatBytes(size);
  const done = formatBytes(have);
  const unit = total.slice(total.lastIndexOf(" "));
  const amount = done.endsWith(unit) && unit !== " bytes" ? done.slice(0, -unit.length) : done;
  const head = `${amount} of ${total}`;
  const left = etaText(size - have, rate);
  return left === null ? head : `${head} · ${left}`;
}

/** "8 s left" / "3 min left", or null without a rate. */
export function etaText(remaining: number, rate: number): string | null {
  if (!(rate > 0) || remaining <= 0) return null;
  const seconds = Math.ceil(remaining / rate);
  if (seconds < 60) return `${seconds} s left`;
  return `${Math.ceil(seconds / 60)} min left`;
}

/** The paused line. `waiting` when it carries on by itself: once the
 *  connection is back, or once `machine` (another computer's label, as
 *  typed) is. A part found after a reload just waits for Resume. */
export function pausedText(have: number, waiting: boolean, machine: string | null = null): string {
  const at = `Paused at ${formatBytes(have)}`;
  if (!waiting) return at;
  return `${at} · waiting for ${machine ?? "the connection"}`;
}

export const QUEUED_TEXT = "Queued";
export const VERIFYING_TEXT = "Checking the file…";
export const DOWNLOADED_TEXT = "Downloaded";
export const ANDROID_OLD_TEXT = "Update TST Desk on this phone to download files.";

/** "Saved to Download/TST Desk" from the phone's `where`, which names the
 *  file too; "Saved to <path>" on the desktop. */
export function savedText(where: string, platform: Platform): string {
  if (platform === "android") {
    const folder = where.includes("/") ? where.slice(0, where.lastIndexOf("/")) : where;
    return `Saved to ${folder}`;
  }
  return `Saved to ${where}`;
}

/** What the status line reads from a card's state. */
export interface StatusView {
  phase: FilePhase;
  have: number;
  rate: number;
  note: string | null;
  waiting: boolean;
  waitingFor: string | null;
  savedTo: string | null;
}

/** Line 2 of a card for its phase (§6.4). A mapped note wins where the
 *  phase's own line is not the whole story. */
export function statusLine(ref: Pick<SentFileRef, "name" | "mime" | "size">, view: StatusView, platform: Platform): string {
  switch (view.phase) {
    case "sending":
      return `Sending ${ref.name}…`;
    case "checking":
    case "idle":
    case "local":
      return view.note ?? sizeLine(ref);
    case "queued":
      return QUEUED_TEXT;
    case "downloading":
      return progressText(view.have, ref.size, view.rate);
    case "paused":
      return view.note ?? pausedText(view.have, view.waiting, view.waitingFor);
    case "verifying":
      return VERIFYING_TEXT;
    case "done":
      return view.note ?? DOWNLOADED_TEXT;
    case "saved":
      return view.note ?? savedText(view.savedTo ?? "", platform);
    case "failed":
      return view.note ?? "Download failed.";
    case "unavailable":
    case "gone":
      return view.note ?? sizeLine(ref);
    case "android-old":
      return ANDROID_OLD_TEXT;
  }
}

// ── APK line (Android, once verified; §6.4) ────────────────────────────

/** `fileStatus().apk`, exactly as the phone reports it (§1.7). */
export interface ApkStatus {
  package: string;
  versionCode: number;
  versionName: string | null;
  isSelf: boolean;
  installedVersionCode: number | null;
  installedVersionName: string | null;
  sameSigner: boolean | null;
}

function versionOf(name: string | null, code: number | null): string {
  if (name !== null && name !== "") return name;
  return code === null ? "" : String(code);
}

/** The installed version equals this one: nothing to install. */
export function apkInstalled(apk: ApkStatus): boolean {
  return apk.installedVersionCode !== null && apk.installedVersionCode === apk.versionCode;
}

/** "Update TST Desk 0.1.8 → 0.1.9", "Installed 0.1.9", or
 *  "Android app com.example.foo 1.2". */
export function apkInfoLine(apk: ApkStatus): string {
  const next = versionOf(apk.versionName, apk.versionCode);
  const label = apk.isSelf ? "TST Desk" : apk.package;
  if (apkInstalled(apk)) return `Installed ${apk.isSelf ? next : `${label} ${next}`}`;
  if (apk.installedVersionCode !== null) {
    const now = versionOf(apk.installedVersionName, apk.installedVersionCode);
    return `Update ${label} ${now} → ${next}`;
  }
  return `Android app ${apk.package} ${next}`;
}

export const SIGNER_WARNING =
  "Signed with a different key than the installed app. Android will refuse it; uninstalling first deletes that app's data (for TST Desk: the paired computer).";
export const DOWNGRADE_WARNING = "Older than the installed version. Android refuses downgrades.";
export const SELF_UPDATE_NOTE = "TST Desk closes to install the update. Open it again after.";
export const INSTALL_PERMISSION_HINT = "Android will ask you to allow TST Desk to install apps.";

/** Warnings stop the automatic install after download (§6.4). */
export function apkWarnings(apk: ApkStatus): string[] {
  const out: string[] = [];
  if (apk.sameSigner === false) out.push(SIGNER_WARNING);
  if (apk.installedVersionCode !== null && apk.versionCode < apk.installedVersionCode) out.push(DOWNGRADE_WARNING);
  return out;
}

/** Plain notes under the APK line: the self-update one and the install
 *  permission hint. */
export function apkNotes(apk: ApkStatus, canInstall: boolean): string[] {
  const out: string[] = [];
  if (apk.isSelf && !apkInstalled(apk)) out.push(SELF_UPDATE_NOTE);
  if (!canInstall && !apkInstalled(apk)) out.push(INSTALL_PERMISSION_HINT);
  return out;
}

// ── Code to copy (§1.8, §6.5) ──────────────────────────────────────────

/** What a code does to a card. `restart`: read status() and start over,
 *  once (`bad_range`). `resync`: carry on from the `have` that came back
 *  (`bad_offset`). `rekey`: ask the phone for a fresh key, once (`bad_key`).
 *  The rest land the card in that phase with the copy. */
export type CodeOutcome =
  | { kind: "phase"; phase: "gone" | "failed" | "unavailable" | "paused" | "done"; copy: string; reveal?: boolean }
  | { kind: "restart" }
  | { kind: "resync" }
  | { kind: "rekey"; copy: string };

/** The code in an error string from the shell: `no_app: <detail>`,
 *  `bad_offset:<have>` and `io: <detail>` carry more after the colon. */
export function codeOf(raw: unknown): string {
  if (typeof raw === "string") {
    const colon = raw.indexOf(":");
    return (colon === -1 ? raw : raw.slice(0, colon)).trim() || "unknown";
  }
  if (raw instanceof Error) return codeOf(raw.message);
  if (raw !== null && typeof raw === "object" && typeof (raw as { code?: unknown }).code === "string") {
    return (raw as { code: string }).code;
  }
  return "unknown";
}

/** `<machine>` in the copy: the bridged session's computer, or this one. */
export interface CopyContext {
  machine: string;
  /** The computer this page talks to (the hub): "this computer" on the
   *  desktop, the paired desktop's name on the phone. Defaults to this one. */
  hub?: string;
  /** The error came from the other computer through the hub (its frame
   *  carries `machine`), not from the hub itself. */
  relayed?: boolean;
  platform: Platform;
  /** The file's name, for the no_app copy's extension. */
  name: string;
  /** The file's size, for the space copy. */
  size: number;
}

/** One line of copy for every code (§6.5). An unknown code reads
 *  "Download failed (<code>)." and never breaks the card. */
export function messageFor(code: string, ctx: CopyContext): CodeOutcome {
  const machine = ctx.machine;
  switch (code) {
    case "artifact_not_found":
      return { kind: "phase", phase: "gone", copy: `This file is no longer on ${machine}.` };
    case "not_found":
      // The desktop shell's code: this computer has no copy (§1.6). For a
      // bridged file only the downloaded copy moved; the other computer
      // still has it, so it can be downloaded again.
      if (machine === THIS_COMPUTER) return { kind: "phase", phase: "gone", copy: `This file is no longer on ${machine}.` };
      return { kind: "phase", phase: "failed", copy: "The copy on this computer was moved or deleted. Download it again." };
    case "not_a_sent_file":
      return { kind: "phase", phase: "failed", copy: "This file can't be downloaded." };
    case "changed":
    case "size_mismatch":
      return { kind: "phase", phase: "failed", copy: `The file changed on ${machine}. Ask for it again.` };
    case "hash_mismatch":
      return { kind: "phase", phase: "failed", copy: "The download was damaged. Try again." };
    case "bad_range":
      return { kind: "restart" };
    case "read_failed":
    case "io":
    case "bad_data":
      return { kind: "phase", phase: "failed", copy: "Couldn't read or write the file. Try again." };
    case "no_space":
    case "too_large":
      return { kind: "phase", phase: "failed", copy: `Not enough space on this device (needs ${formatBytes(ctx.size)}).` };
    case "bad_offset":
      return { kind: "resync" };
    case "bad_key":
      return { kind: "rekey", copy: "Restart TST Desk and try again." };
    case "not_on_remote":
      // Only the hub sends this: its forward list predates the verb.
      return { kind: "phase", phase: "unavailable", copy: hubUpdateCopy(ctx) };
    case "unknown_message":
      return {
        kind: "phase",
        phase: "unavailable",
        copy: ctx.relayed === true ? `Update TST Desk on ${machine} to download this.` : hubUpdateCopy(ctx),
      };
    case "not_allowed":
      // The other computer's peer policy refused the hub: that one is old.
      return { kind: "phase", phase: "unavailable", copy: `Update TST Desk on ${machine} to download this.` };
    case "machine_not_found":
      return { kind: "phase", phase: "paused", copy: `${capitalized(machine)} is not reachable.` };
    case "machine_unreachable":
      // The hub's answer while that computer sleeps, is offline or is parked
      // (refused, locked, outdated). The card waits for it, as the
      // composer's and the approval card's copy do.
      return { kind: "phase", phase: "paused", copy: `${capitalized(machine)} isn't reachable. The download carries on when it is.` };
    case "no_app": {
      if (ctx.platform === "android") {
        const ext = extensionOf(ctx.name);
        const what = ext === "" ? "these" : ext;
        return { kind: "phase", phase: "done", copy: `No app here opens ${what} files. Save it to Downloads instead.` };
      }
      return { kind: "phase", phase: "done", copy: "No app opens this file.", reveal: true };
    }
    case "not_verified":
      return { kind: "phase", phase: "failed", copy: "Download it again first." };
    case "not_openable":
      return {
        kind: "phase",
        phase: "done",
        copy: "TST Desk doesn't open this kind of file. Show it in its folder instead.",
        reveal: true,
      };
    case "needs_android_10":
      return {
        kind: "phase",
        phase: "done",
        copy: "Saving to Downloads needs Android 10 or later. Open it from here instead.",
      };
    default:
      return { kind: "phase", phase: "failed", copy: `Download failed (${code}).` };
  }
}

/** The hub refused the verb, so the hub needs the update, not the computer
 *  that sent the file, which proved it is new by sending it (§4.5). */
function hubUpdateCopy(ctx: CopyContext): string {
  const hub = ctx.hub ?? THIS_COMPUTER;
  if (ctx.machine === THIS_COMPUTER || ctx.machine === hub) return `Update TST Desk on ${hub} to download this.`;
  return `Update TST Desk on ${hub} to download files from ${ctx.machine}.`;
}

/** "This file is no longer on <machine>." without a code, for a probe
 *  that finds nothing to show. */
export function goneText(machine: string): string {
  return `This file is no longer on ${machine}.`;
}

/** The copy for a machine label: another computer by name, or this one. */
export const THIS_COMPUTER = "this computer";

/** The label for a bridged session's computer the list doesn't have. */
export const ANOTHER_COMPUTER = "another computer";

/** A machine label at the start of a sentence. The app's own labels take a
 *  capital; a computer's name reads exactly as typed ("iMac", "studio-mac"),
 *  as it does in the composer's notice and on the approval card. */
function capitalized(machine: string): string {
  if (machine !== THIS_COMPUTER && machine !== ANOTHER_COMPUTER) return machine;
  return machine.charAt(0).toUpperCase() + machine.slice(1);
}
