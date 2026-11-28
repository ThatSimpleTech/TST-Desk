// @vitest-environment jsdom
//
// SECUX2-01: a project's own instructions wait on the folder's trust. The
// card lists them and sends the answer; a new kind says so; Settings lists
// trusted folders with Revoke.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ProtocolClient } from "./client";
import type { DaemonEventUnion, FolderTrust } from "./protocol";

const sent = vi.hoisted(() => [] as unknown[]);
vi.mock("./connection-status.svelte.js", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  sendToDaemon: (msg: unknown) => {
    sent.push(msg);
    return true;
  },
}));

import FolderTrustCard from "./components/FolderTrustCard.svelte";
import TrustedFolders from "./components/TrustedFolders.svelte";
import { bindClient, ingestEvent, resetSession } from "./session-status.svelte.js";
import { resetSettings, settings } from "./settings.svelte.js";

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;
let clientSent: unknown[] = [];

function wall(trust: FolderTrust | null): void {
  ingestEvent({
    type: "boundary_update",
    session_id: "s1",
    seq: 2,
    writable_paths: ["**"],
    allowed_commands: [],
    network: "deny",
    spend_usd: 25,
    wall_clock_hours: 8,
    max_iterations: 200,
    source: "defaults",
    config_trust: null,
    folder_trust: trust,
  } as DaemonEventUnion);
  flushSync();
}

function button(label: string): HTMLButtonElement | undefined {
  return [...target.querySelectorAll("button")].find((b) => b.textContent?.includes(label));
}

beforeEach(() => {
  resetSession();
  resetSettings();
  sent.length = 0;
  clientSent = [];
  bindClient({ send: (m: unknown) => (clientSent.push(m), true), attach: () => undefined } as unknown as ProtocolClient);
  ingestEvent({ type: "session_state", session_id: "s1", state: "idle", seq: 1 } as DaemonEventUnion);
  target = document.createElement("div");
  document.body.appendChild(target);
});

afterEach(() => {
  if (component !== null) unmount(component);
  component = null;
  target.remove();
  bindClient(null);
});

describe("the folder trust card", () => {
  it("lists the files and sends the answer", () => {
    component = mount(FolderTrustCard, { target });
    wall({
      folder: "/work/repo",
      state: "pending",
      files: [
        { kind: "instructions", path: "AGENTS.md" },
        { kind: "rules", path: ".tst/rules/style.md" },
      ],
    });
    expect(target.textContent).toContain("Load this project's instructions?");
    expect(target.textContent).toContain("AGENTS.md");
    expect(target.textContent).toContain(".tst/rules/style.md");
    button("Trust this folder")?.click();
    expect(clientSent).toContainEqual({ type: "decide_folder_trust", session_id: "s1", trust: true });
  });

  it("says when only new kinds wait", () => {
    component = mount(FolderTrustCard, { target });
    wall({
      folder: "/work/repo",
      state: "pending",
      files: [{ kind: "skills", path: ".tst/skills/deploy/SKILL.md" }],
      trusted_kinds: ["instructions"],
    });
    expect(target.textContent).toContain("New instructions in this project");
    expect(target.textContent).not.toContain("AGENTS.md");
  });

  it("is gone once nothing waits", () => {
    component = mount(FolderTrustCard, { target });
    wall(null);
    expect(target.querySelector(".trust-bar")).toBeNull();
  });
});

describe("trusted folders in Settings", () => {
  it("lists each folder with Revoke", async () => {
    settings.trustedFolders = [{ folder: "/work/repo", kinds: ["instructions", "rules"] }];
    component = mount(TrustedFolders, { target });
    flushSync();
    expect(target.textContent).toContain("/work/repo");
    expect(target.textContent).toContain("instructions, rules");
    expect(target.querySelector('[aria-label="Revoke trust for /work/repo"]')).not.toBeNull();
  });
});
