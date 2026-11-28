// UPGRADE-02: a data folder a newer version wrote is read-only here. The
// window says so in a standing banner, and a refused change says why.

import { afterEach, describe, expect, it } from "vitest";
import type { DaemonEventUnion } from "./protocol";
import { banners, clearNotifications, notifyEvent } from "./notifications.svelte.js";
import { daemonErrorCopy } from "./error-copy";

afterEach(() => clearNotifications());

describe("a newer version's data folder", () => {
  it("shows a standing banner naming the version", () => {
    notifyEvent({ type: "setup_state", seq: 1, data_written_by: "0.6.0" } as unknown as DaemonEventUnion);
    const banner = banners.find((b) => b.title === "Your data was written by a newer TST Desk");
    expect(banner?.body).toContain("Data was written by 0.6.0. Update TST Desk to keep working; nothing will be changed until then.");
  });

  it("says nothing on an ordinary folder", () => {
    notifyEvent({ type: "setup_state", seq: 1 } as unknown as DaemonEventUnion);
    expect(banners).toHaveLength(0);
  });

  it("turns a refused change into a toast with the daemon's words", () => {
    const copy = daemonErrorCopy("data_read_only", "This data folder was written by TST Desk 0.6.0.");
    expect(copy.severity).toBe("toast");
    expect(copy.body).toContain("0.6.0");
  });
});
