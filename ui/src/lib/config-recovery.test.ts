// SETMODEL-04: when config.yaml did not load at start, the window keeps a
// banner up until it loads again, and Settings can ask for the last copy.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { DaemonEventUnion } from "./protocol";

const sent = vi.hoisted(() => [] as unknown[]);
vi.mock("./connection-status.svelte.js", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  sendToDaemon: (msg: unknown) => {
    sent.push(msg);
    return true;
  },
}));

import { banners, clearNotifications, notifyEvent } from "./notifications.svelte.js";
import { restoreLastGoodConfig } from "./settings.svelte.js";

function setup(over: Record<string, unknown>): DaemonEventUnion {
  return { type: "setup_state", seq: 1, ...over } as unknown as DaemonEventUnion;
}

afterEach(() => clearNotifications());

describe("a config.yaml that did not load", () => {
  it("keeps a banner up until it loads again", () => {
    notifyEvent(setup({ config_error: "Invalid YAML at line 3", config_running_on: "last_good" }));
    const banner = banners.find((b) => b.title === "config.yaml could not be loaded");
    expect(banner?.body).toContain("the last copy that worked");
    notifyEvent(setup({ config_error: null }));
    expect(banners.some((b) => b.title === "config.yaml could not be loaded")).toBe(false);
  });

  it("says when the app runs on the shipped defaults", () => {
    notifyEvent(setup({ config_error: "empty", config_running_on: "defaults" }));
    expect(banners[0]?.body).toContain("the shipped defaults");
  });

  it("asks the daemon to restore the last working copy", () => {
    restoreLastGoodConfig();
    expect(sent).toContainEqual({ type: "restore_last_good_config" });
  });
});
