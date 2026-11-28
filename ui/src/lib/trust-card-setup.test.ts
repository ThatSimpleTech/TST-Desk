// @vitest-environment jsdom
//
// STATES1-08: "Trust and apply" runs the repository's worktree.setup at once,
// and again in every new chat. The card listed it as one row among many,
// styled like a spend cap; now it says plainly what trusting runs.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { ProtocolClient } from "./client";
import type { DaemonEventUnion } from "./protocol";

import WorkspaceTrustCard from "./components/WorkspaceTrustCard.svelte";
import { bindClient, ingestEvent, resetSession } from "./session-status.svelte.js";

function wall(setup: string | null): void {
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
    source: "defaults — workspace config not trusted yet",
    config_trust: {
      state: "pending",
      sha256: "c".repeat(64),
      loosened: [
        ...(setup ? [{ key: "worktree.setup", value: setup, default: "none" }] : []),
        { key: "boundary.network", value: "example.com", default: "deny" },
      ],
      setup_command: setup,
    },
  } as DaemonEventUnion);
  flushSync();
}

let target: HTMLElement;
let component: ReturnType<typeof mount> | null = null;

beforeEach(() => {
  resetSession();
  bindClient({ send: () => true, attach: () => undefined } as unknown as ProtocolClient);
  ingestEvent({ type: "session_state", session_id: "s1", state: "idle", seq: 1 } as DaemonEventUnion);
  target = document.createElement("div");
  document.body.appendChild(target);
  component = mount(WorkspaceTrustCard, { target });
  flushSync();
});

afterEach(() => {
  if (component !== null) unmount(component);
  component = null;
  target.remove();
  bindClient(null);
});

describe("the trust card and worktree.setup", () => {
  it("names the command and says when it runs", () => {
    wall("npm ci && ./scripts/bootstrap.sh");
    const runs = target.querySelector(".runs");
    expect(runs?.textContent).toContain("Trusting runs this command");
    expect(runs?.querySelector("code")?.textContent).toBe("npm ci && ./scripts/bootstrap.sh");
    expect(runs?.textContent).toContain("again each time a new chat starts");
  });

  it("says nothing of the kind when the file runs nothing", () => {
    wall(null);
    expect(target.querySelector(".runs")).toBeNull();
  });
});
