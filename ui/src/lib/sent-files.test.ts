// Sent-file cards, the pure half (TD-5144): kinds, the button matrix of
// design §6.4, progress copy, the APK line, and one line of copy for every
// code in §1.8/§6.5.

import { describe, expect, it } from "vitest";
import fixtures from "./protocol-fixtures.json";
import type { ArtifactEntry, ArtifactReady } from "./protocol";
import {
  actionLabel,
  apkInfoLine,
  apkNotes,
  apkWarnings,
  cardLabel,
  codeOf,
  detectPlatform,
  DOWNGRADE_WARNING,
  etaText,
  extensionOf,
  fileActions,
  fileKind,
  INSTALL_PERMISSION_HINT,
  kindLabel,
  messageFor,
  nameParts,
  pausedText,
  progressText,
  savedText,
  SELF_UPDATE_NOTE,
  sendingText,
  sentFileFromEvent,
  SIGNER_WARNING,
  sizeLine,
  statusLine,
  type ApkStatus,
  type CopyContext,
  type FileKind,
  type FilePhase,
  type Platform,
} from "./sent-files";

const READY = fixtures.artifact_ready as ArtifactReady;
const MB = 1_000_000;

describe("sentFileFromEvent", () => {
  it("reads the fixture's sent_file event", () => {
    expect(sentFileFromEvent(READY, "sess-1")).toEqual({
      artifactId: "0".repeat(32),
      sessionId: "sess-1",
      name: "app-release.apk",
      size: 2,
      mime: "application/vnd.android.package-archive",
      sha256: READY.sha256,
      caption: "0.1.9 release build",
      toolCallId: "call_abc",
    });
  });

  it("ignores a TD-3201 artifact, with kind or without", () => {
    const list = fixtures.artifact_list.artifacts as ArtifactEntry[];
    expect(sentFileFromEvent(list[0], "sess-1")).toBeNull();
    const { kind: _kind, ...plain } = READY;
    expect(sentFileFromEvent(plain as ArtifactReady, "sess-1")).toBeNull();
    expect(sentFileFromEvent(list[1], "sess-1")?.name).toBe("app-release.apk");
  });

  it("refuses a malformed sent_file instead of drawing half a card", () => {
    expect(sentFileFromEvent({ ...READY, artifact_id: "../x" }, "s")).toBeNull();
    expect(sentFileFromEvent({ ...READY, size: null }, "s")).toBeNull();
    expect(sentFileFromEvent({ ...READY, size: -1 }, "s")).toBeNull();
    expect(sentFileFromEvent({ ...READY, sha256: "abc" }, "s")).toBeNull();
    expect(sentFileFromEvent({ ...READY, name: "" }, "s")).toBeNull();
    expect(sentFileFromEvent({ ...READY, caption: "" }, "s")?.caption).toBeNull();
  });
});

describe("kinds and labels", () => {
  const cases: Array<[string, string, FileKind, string]> = [
    ["app-release.apk", "application/vnd.android.package-archive", "apk", "Android app"],
    ["shot.png", "image/png", "image", "Image"],
    ["report.pdf", "application/pdf", "pdf", "PDF"],
    ["bundle.zip", "application/zip", "archive", "Archive"],
    ["logs.tar.gz", "application/gzip", "archive", "Archive"],
    ["demo.mp4", "video/mp4", "video", "Video"],
    ["note.m4a", "audio/mp4", "audio", "Audio"],
    ["notes.md", "text/markdown", "text", "Text"],
    ["data.json", "application/json", "text", "Text"],
    ["blob.bin", "application/octet-stream", "other", "File"],
    ["setup.exe", "application/octet-stream", "program", "Program"],
  ];
  it.each(cases)("%s is %s", (name, mime, kind, label) => {
    expect(fileKind(name, mime)).toBe(kind);
    expect(kindLabel(fileKind(name, mime))).toBe(label);
  });

  it("knows every program extension, in any case", () => {
    for (const ext of [
      "exe", "msi", "bat", "cmd", "com", "ps1", "sh", "command", "app",
      "AppImage", "desktop", "jar", "deb", "rpm", "pkg", "dmg", "run",
    ]) {
      expect(fileKind(`thing.${ext}`, "application/octet-stream"), ext).toBe("program");
    }
    expect(fileKind("installer.dmg", "application/x-apple-diskimage")).toBe("program");
    expect(fileKind("script.sh.txt", "text/plain")).toBe("text");
  });

  it("takes the extension from the last dot, never from a leading one", () => {
    expect(extensionOf(".env")).toBe("");
    expect(extensionOf("a.TAR.GZ")).toBe(".gz");
    expect(extensionOf("noext")).toBe("");
  });

  it("writes the size line, the group label and the skeleton", () => {
    const ref = { name: "app-release.apk", mime: "application/vnd.android.package-archive", size: 46_241_280 };
    expect(sizeLine(ref)).toBe("Android app · 46.2 MB");
    expect(cardLabel(ref)).toBe("File: app-release.apk, 46.2 MB");
    expect(sendingText(["android/app/build/outputs/apk/release/app-release.apk"])).toBe("Sending app-release.apk…");
    expect(sendingText(["dist\\app-release.apk", "notes.pdf"])).toBe("Sending app-release.apk and 1 more…");
    expect(sendingText(undefined)).toBeNull();
    expect(sendingText([])).toBeNull();
  });

  it("keeps the extension on screen when the name ellipsizes", () => {
    const long = `${"x".repeat(60)}.apk`;
    const parts = nameParts(long);
    expect(parts.head + parts.tail).toBe(long);
    expect(parts.tail.endsWith(".apk")).toBe(true);
    expect(nameParts("short.apk")).toEqual({ head: "short.apk", tail: "" });
  });
});

describe("platform", () => {
  it("is android with TstDeskFiles v1, android-old without it, desktop in Tauri, else browser", () => {
    expect(detectPlatform({ TstDeskFiles: { filesVersion: () => 1 } })).toBe("android");
    expect(detectPlatform({ TstDeskFiles: { filesVersion: () => 0 } })).toBe("android-old");
    expect(detectPlatform({ TstDeskNative: {} })).toBe("android-old");
    expect(detectPlatform({ __TAURI_INTERNALS__: {} })).toBe("desktop");
    expect(detectPlatform({})).toBe("browser");
  });
});

describe("action matrix (§6.4)", () => {
  const platforms: Platform[] = ["android", "android-old", "desktop", "browser"];
  const kinds: FileKind[] = ["apk", "image", "pdf", "archive", "video", "audio", "text", "program", "other"];
  const phases: FilePhase[] = [
    "sending", "checking", "idle", "local", "queued", "downloading", "paused", "verifying",
    "done", "saved", "failed", "unavailable", "gone", "android-old",
  ];

  it("never offers Open for a program on a computer, and nothing at all on an old phone", () => {
    for (const platform of platforms) {
      for (const kind of kinds) {
        for (const phase of phases) {
          for (const local of [false, true]) {
            const set = fileActions(platform, kind, phase, { local });
            const all = [set.primary, ...set.menu];
            // §6.4 and R17: the computer never opens an agent-made program.
            // The phone's column has no such exception (Android can't run one).
            if (kind === "program" && platform !== "android") {
              expect(all, `${platform}/${phase}`).not.toContain("open");
            }
            if (platform === "android-old") expect(set).toEqual({ primary: null, menu: [] });
            for (const action of all) if (action !== null) expect(actionLabel(action)).not.toBe("");
          }
        }
      }
    }
  });

  it("offers nothing while sending, checking, verifying, unavailable", () => {
    for (const platform of platforms) {
      for (const phase of ["sending", "checking", "verifying", "unavailable", "android-old"] as FilePhase[]) {
        expect(fileActions(platform, "apk", phase)).toEqual({ primary: null, menu: [] });
      }
    }
  });

  it("idle: Download on desktop and browser, Download and install/open on the phone", () => {
    expect(fileActions("desktop", "pdf", "idle")).toEqual({ primary: "download", menu: [] });
    expect(fileActions("browser", "pdf", "idle")).toEqual({ primary: "download", menu: [] });
    expect(fileActions("android", "apk", "idle")).toEqual({ primary: "download-install", menu: ["save-downloads"] });
    expect(fileActions("android", "pdf", "idle")).toEqual({ primary: "download-open", menu: ["save-downloads"] });
  });

  it("local (desktop only): Open, Show in folder, Save a copy; a program only shows in folder", () => {
    expect(fileActions("desktop", "pdf", "local")).toEqual({ primary: "open", menu: ["reveal", "save-as"] });
    expect(fileActions("desktop", "program", "local")).toEqual({ primary: "reveal", menu: ["save-as"] });
    expect(fileActions("android", "pdf", "local")).toEqual({ primary: null, menu: [] });
    expect(fileActions("browser", "pdf", "local")).toEqual({ primary: null, menu: [] });
  });

  it("queued and downloading: Cancel", () => {
    for (const platform of ["desktop", "android", "browser"] as Platform[]) {
      expect(fileActions(platform, "apk", "queued")).toEqual({ primary: "cancel", menu: [] });
      expect(fileActions(platform, "apk", "downloading")).toEqual({ primary: "cancel", menu: [] });
    }
  });

  it("paused: Resume, with Cancel in the menu except in a browser", () => {
    expect(fileActions("desktop", "apk", "paused")).toEqual({ primary: "resume", menu: ["cancel"] });
    expect(fileActions("android", "apk", "paused")).toEqual({ primary: "resume", menu: ["cancel"] });
    expect(fileActions("browser", "apk", "paused")).toEqual({ primary: "resume", menu: [] });
  });

  it("done: Open or Install, the phone's menu, and only the line in a browser", () => {
    expect(fileActions("desktop", "pdf", "done")).toEqual({ primary: "open", menu: ["reveal"] });
    expect(fileActions("desktop", "program", "done")).toEqual({ primary: "reveal", menu: [] });
    expect(fileActions("android", "apk", "done")).toEqual({ primary: "install", menu: ["save-downloads", "remove"] });
    expect(fileActions("android", "apk", "done", { warning: true }).primary).toBe("install-anyway");
    expect(fileActions("android", "pdf", "done")).toEqual({ primary: "open", menu: ["save-downloads", "remove"] });
    expect(fileActions("browser", "pdf", "done")).toEqual({ primary: null, menu: [] });
  });

  it("saved keeps the desktop's buttons; the phone offers Open", () => {
    expect(fileActions("desktop", "pdf", "saved", { local: true })).toEqual(fileActions("desktop", "pdf", "local"));
    expect(fileActions("desktop", "pdf", "saved")).toEqual(fileActions("desktop", "pdf", "done"));
    expect(fileActions("android", "apk", "saved")).toEqual({ primary: "open", menu: [] });
    expect(fileActions("browser", "pdf", "saved")).toEqual({ primary: null, menu: [] });
  });

  it("failed: Retry; gone: nothing but the phone's Remove for a part", () => {
    for (const platform of ["desktop", "android", "browser"] as Platform[]) {
      expect(fileActions(platform, "pdf", "failed")).toEqual({ primary: "retry", menu: [] });
      expect(fileActions(platform, "pdf", "gone")).toEqual({ primary: null, menu: [] });
    }
    expect(fileActions("android", "pdf", "gone", { partial: true })).toEqual({ primary: null, menu: ["remove"] });
  });

  it("desktop no_app makes Show in folder the button", () => {
    expect(fileActions("desktop", "other", "local", { reveal: true }).primary).toBe("reveal");
    expect(fileActions("desktop", "other", "done", { reveal: true }).primary).toBe("reveal");
  });

  it("labels the buttons as the design does", () => {
    expect(actionLabel("download-install")).toBe("Download and install");
    expect(actionLabel("reveal")).toBe("Show in folder");
    expect(actionLabel("save-as")).toBe("Save a copy…");
    expect(actionLabel("install-anyway")).toBe("Install anyway");
    expect(actionLabel("remove")).toBe("Remove from phone");
  });
});

describe("progress copy", () => {
  it("reads have of size and the time left", () => {
    expect(progressText(12.4 * MB, 46.2 * MB, 4.225 * MB)).toBe("12.4 of 46.2 MB · 8 s left");
    expect(progressText(12.4 * MB, 46.2 * MB, 0)).toBe("12.4 of 46.2 MB");
    expect(progressText(500, 46.2 * MB, 0)).toBe("500 bytes of 46.2 MB");
    expect(progressText(512_000, 900_000, 0)).toBe("512.0 of 900.0 KB");
  });

  it("rounds the ETA up, and switches to minutes", () => {
    expect(etaText(10 * MB, 1 * MB)).toBe("10 s left");
    expect(etaText(10 * MB, 3 * MB)).toBe("4 s left");
    expect(etaText(200 * MB, 1 * MB)).toBe("4 min left");
    expect(etaText(0, 1 * MB)).toBeNull();
    expect(etaText(10, Number.NaN)).toBeNull();
  });

  it("writes the paused, saved and status lines", () => {
    expect(pausedText(12.4 * MB, true)).toBe("Paused at 12.4 MB · waiting for the connection");
    expect(pausedText(12.4 * MB, false)).toBe("Paused at 12.4 MB");
    expect(savedText("Download/TST Desk/app-release.apk", "android")).toBe("Saved to Download/TST Desk");
    expect(savedText("/home/u/Downloads/a.pdf", "desktop")).toBe("Saved to /home/u/Downloads/a.pdf");
    const ref = { name: "app-release.apk", mime: "application/vnd.android.package-archive", size: 46.2 * MB };
    const view = { have: 0, rate: 0, note: null, waiting: false, savedTo: null };
    expect(statusLine(ref, { ...view, phase: "idle" }, "android")).toBe("Android app · 46.2 MB");
    expect(statusLine(ref, { ...view, phase: "queued" }, "android")).toBe("Queued");
    expect(statusLine(ref, { ...view, phase: "verifying" }, "android")).toBe("Checking the file…");
    expect(statusLine(ref, { ...view, phase: "done" }, "android")).toBe("Downloaded");
    expect(statusLine(ref, { ...view, phase: "android-old" }, "android-old")).toBe(
      "Update TST Desk on this phone to download files.",
    );
    expect(statusLine(ref, { ...view, phase: "failed", note: "x" }, "desktop")).toBe("x");
  });
});

describe("APK line", () => {
  const base: ApkStatus = {
    package: "com.thatsimpletech.tstdesk",
    versionCode: 10,
    versionName: "0.1.9",
    isSelf: true,
    installedVersionCode: 9,
    installedVersionName: "0.1.8",
    sameSigner: true,
  };

  it("names a self-update and says the app closes", () => {
    expect(apkInfoLine(base)).toBe("Update TST Desk 0.1.8 → 0.1.9");
    expect(apkWarnings(base)).toEqual([]);
    expect(apkNotes(base, true)).toEqual([SELF_UPDATE_NOTE]);
    expect(apkNotes(base, false)).toEqual([SELF_UPDATE_NOTE, INSTALL_PERMISSION_HINT]);
  });

  it("says Installed when the same version is already on the phone", () => {
    const same = { ...base, installedVersionCode: 10, installedVersionName: "0.1.9" };
    expect(apkInfoLine(same)).toBe("Installed 0.1.9");
    expect(apkNotes(same, false)).toEqual([]);
  });

  it("names a new app, whose install state Android does not tell us", () => {
    const other: ApkStatus = {
      package: "com.example.foo",
      versionCode: 3,
      versionName: "1.2",
      isSelf: false,
      installedVersionCode: null,
      installedVersionName: null,
      sameSigner: null,
    };
    expect(apkInfoLine(other)).toBe("Android app com.example.foo 1.2");
    expect(apkWarnings(other)).toEqual([]);
    expect(apkNotes(other, true)).toEqual([]);
  });

  it("warns about a different signer and about a downgrade", () => {
    expect(apkWarnings({ ...base, sameSigner: false })).toEqual([SIGNER_WARNING]);
    const older = { ...base, versionCode: 8, versionName: "0.1.7" };
    expect(apkWarnings(older)).toEqual([DOWNGRADE_WARNING]);
    expect(apkWarnings({ ...older, sameSigner: false })).toEqual([SIGNER_WARNING, DOWNGRADE_WARNING]);
  });
});

describe("code to copy (§6.5)", () => {
  const remote: CopyContext = { machine: "studio-mac", platform: "android", name: "app-release.apk", size: 46.2 * MB };
  const desk: CopyContext = { machine: "this computer", platform: "desktop", name: "notes.bin", size: 1200 };

  function copy(code: string, ctx: CopyContext = remote): string {
    const out = messageFor(code, ctx);
    if (out.kind === "phase" || out.kind === "rekey") return out.copy;
    return out.kind;
  }

  function phase(code: string, ctx: CopyContext = remote): string {
    const out = messageFor(code, ctx);
    return out.kind === "phase" ? out.phase : out.kind;
  }

  it("maps every daemon, shell and phone code to its line", () => {
    expect([phase("artifact_not_found"), copy("artifact_not_found")]).toEqual(["gone", "This file is no longer on studio-mac."]);
    expect(copy("not_found", desk)).toBe("This file is no longer on this computer.");
    expect([phase("not_a_sent_file"), copy("not_a_sent_file")]).toEqual(["failed", "This file can't be downloaded."]);
    for (const code of ["changed", "size_mismatch"]) {
      expect([phase(code), copy(code)]).toEqual(["failed", "The file changed on studio-mac. Ask for it again."]);
    }
    expect([phase("hash_mismatch"), copy("hash_mismatch")]).toEqual(["failed", "The download was damaged. Try again."]);
    expect(messageFor("bad_range", remote)).toEqual({ kind: "restart" });
    for (const code of ["read_failed", "io", "bad_data"]) {
      expect([phase(code), copy(code)]).toEqual(["failed", "Couldn't read or write the file. Try again."]);
    }
    for (const code of ["no_space", "too_large"]) {
      expect([phase(code), copy(code)]).toEqual(["failed", "Not enough space on this device (needs 46.2 MB)."]);
    }
    expect(messageFor("bad_offset", remote)).toEqual({ kind: "resync" });
    expect(messageFor("bad_key", remote)).toEqual({ kind: "rekey", copy: "Restart TST Desk and try again." });
    for (const code of ["not_on_remote", "unknown_message", "not_allowed"]) {
      expect([phase(code), copy(code)]).toEqual(["unavailable", "Update TST Desk on studio-mac to download this."]);
    }
    expect([phase("machine_not_found"), copy("machine_not_found")]).toEqual(["paused", "studio-mac is not reachable."]);
    expect(copy("machine_not_found", desk)).toBe("This computer is not reachable.");
    expect([phase("no_app"), copy("no_app")]).toEqual(["done", "No app here opens .apk files. Save it to Downloads instead."]);
    expect(messageFor("no_app", desk)).toEqual({ kind: "phase", phase: "done", copy: "No app opens this file.", reveal: true });
    expect([phase("not_verified"), copy("not_verified")]).toEqual(["failed", "Download it again first."]);
    expect([phase("needs_android_10"), copy("needs_android_10")]).toEqual([
      "done",
      "Saving to Downloads needs Android 10 or later. Open it from here instead.",
    ]);
  });

  it("falls back to Download failed (code) for anything else", () => {
    for (const code of ["bad_id", "bad_meta", "wat", ""]) {
      expect([phase(code), copy(code)]).toEqual(["failed", `Download failed (${code}).`]);
    }
  });

  it("reads the code out of a shell error string", () => {
    expect(codeOf("bad_offset:1048576")).toBe("bad_offset");
    expect(codeOf("no_app: xdg-open exited 3")).toBe("no_app");
    expect(codeOf("io: disk full")).toBe("io");
    expect(codeOf("hash_mismatch")).toBe("hash_mismatch");
    expect(codeOf(new Error("too_large"))).toBe("too_large");
    expect(codeOf({ code: "bad_key" })).toBe("bad_key");
    expect(codeOf(42)).toBe("unknown");
  });
});
