// UPGRADE-02: config.yaml keys this version ignores are named once per
// window, so a typo there is never silent.

import { afterEach, describe, expect, it } from "vitest";
import type { DaemonEventUnion } from "./protocol";
import { clearNotifications, notifyEvent, toasts } from "./notifications.svelte.js";
import { unknownConfigKeysNotice } from "./error-copy";

function setup(keys: string[]): DaemonEventUnion {
  return { type: "setup_state", seq: 1, config_unknown_keys: keys } as unknown as DaemonEventUnion;
}

afterEach(() => clearNotifications());

describe("unknown config keys", () => {
  it("are named once, however often setup_state comes", () => {
    notifyEvent(setup(["autonomy.verify_budget_usd"]));
    expect(toasts.some((t) => t.body.includes("autonomy.verify_budget_usd"))).toBe(true);
    clearNotifications();
    notifyEvent(setup(["autonomy.verify_budget_usd"]));
    expect(toasts).toHaveLength(0);
  });

  it("shortens a long list", () => {
    const body = unknownConfigKeysNotice(["a.b", "c.d", "e.f", "g.h", "i.j"]).body;
    expect(body).toContain("a.b, c.d, e.f and 2 more");
  });
});
