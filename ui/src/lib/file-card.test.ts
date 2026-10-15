// @vitest-environment jsdom
//
// One file card, mounted for real on the desktop (TD-5144, review finding 2):
// Open shows only for a name on the allowlist, read the way the shell saves
// it (trailing spaces and dots gone); anything else gets Show in folder and
// Save a copy. Only the shell's invoke and the socket are faked.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";

const tauri = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: tauri.invoke }));
vi.mock("./machines.svelte.js", () => ({ machineNameFor: () => null, machineById: () => null }));
vi.mock("./connection-status.svelte.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
  return {
    ...real,
    onDaemonEvent: () => () => {},
    onConnectionState: () => () => {},
    onResume: () => () => {},
    sendToDaemon: () => true,
  };
});

import FileCard from "./components/chat/FileCard.svelte";
import { configureDownloads, resetDownloads } from "./downloads.svelte";
import { createTauriHost } from "./file-sink";
import type { SentFileRef } from "./sent-files";

function sent(n: number, name: string, mime = "application/octet-stream"): SentFileRef {
  return {
    artifactId: String(n).repeat(32),
    sessionId: "sess-1",
    name,
    size: 2_000_000,
    mime,
    sha256: "ab".repeat(32),
    caption: null,
    toolCallId: "call_abc",
  };
}

let app: ReturnType<typeof mount> | null = null;

async function render(file: SentFileRef): Promise<void> {
  app = mount(FileCard, { target: document.body, props: { file } });
  flushSync();
  await vi.waitFor(() => {
    flushSync();
    expect(document.querySelector('.file-card[data-phase="local"]')).not.toBeNull();
  });
}

/** The primary button and, after opening the menu, its items. */
function offered(): string[] {
  document.querySelector<HTMLButtonElement>("button.more")?.click();
  flushSync();
  return [...document.querySelectorAll("button")].map((b) => b.textContent?.trim() ?? "").filter((t) => t !== "");
}

beforeEach(() => {
  tauri.invoke.mockReset();
  tauri.invoke.mockImplementation(async (cmd: string) => (cmd === "sent_file_status" ? { state: "local", have: 0 } : null));
  resetDownloads();
  configureDownloads({ platform: "desktop", host: createTauriHost(), send: () => true, machine: () => "this computer" });
});

afterEach(() => {
  if (app !== null) {
    unmount(app);
    app = null;
  }
  resetDownloads();
  document.body.innerHTML = "";
});

describe("a file card on the desktop", () => {
  it("offers Open for a PDF", async () => {
    await render(sent(1, "notes.pdf", "application/pdf"));
    expect(offered()).toEqual(["Open", "Show in folder", "Save a copy…"]);
  });

  it("never offers Open for a script the agent sent", async () => {
    await render(sent(2, "setup.js", "text/javascript"));
    expect(offered()).toEqual(["Show in folder", "Save a copy…"]);
    document.querySelector<HTMLButtonElement>("button.primary")?.click();
    await vi.waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("sent_file_reveal", { sessionId: "sess-1", artifactId: "2".repeat(32) }),
    );
    expect(tauri.invoke).not.toHaveBeenCalledWith("sent_file_open", expect.anything());
  });

  it("reads the kind from the name the shell saves: 'x.exe ' is a program", async () => {
    await render(sent(3, "x.exe "));
    expect(document.querySelector(".status")?.textContent).toBe("Program · 2.0 MB");
    expect(offered()).toEqual(["Show in folder", "Save a copy…"]);
  });
});
