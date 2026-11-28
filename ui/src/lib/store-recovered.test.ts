// ERR2B-04: when a store file could not be read, the computer's window says
// so once, as a banner, with what came back and where the file was kept.

import { describe, expect, it } from "vitest";
import { storeRecoveredNotice } from "./error-copy";

describe("store_recovered", () => {
  it("names what came back and where the damaged file is", () => {
    const notice = storeRecoveredNotice({
      store: "sessions",
      kept_as: "sessions.json.damaged-20261128T040000Z",
      from_backup: 3,
      rebuilt: 1,
    });
    expect(notice.severity).toBe("banner");
    expect(notice.title).toBe("Your chat list file could not be read");
    expect(notice.body).toContain("Recovered 3 from the last good copy and 1 rebuilt from their folders.");
    expect(notice.body).toContain("sessions.json.damaged-20261128T040000Z");
    expect(notice.body).toContain("will not be written over");
  });

  it("says when nothing could be recovered", () => {
    const notice = storeRecoveredNotice({ store: "jobs", kept_as: "jobs.json.damaged-x", from_backup: 0, rebuilt: 0 });
    expect(notice.title).toBe("Your scheduled jobs file could not be read");
    expect(notice.body.startsWith("Nothing could be recovered from it.")).toBe(true);
  });

  it("names a damaged audit log and where it was kept (ERR2B-06)", () => {
    const n = storeRecoveredNotice({ store: "audit", kept_as: "audit.db.damaged-x", from_backup: 0, rebuilt: 0 });
    expect(n.title).toBe("The audit log was damaged");
    expect(n.body).toContain("audit.db.damaged-x");
  });

  it("says when this run's audit log is in memory only (ERR2B-06)", () => {
    const n = storeRecoveredNotice({ store: "audit", kept_as: "", from_backup: 0, rebuilt: 0, memory_only: true });
    expect(n.title).toBe("The audit log could not be opened");
    expect(n.body).toContain("in memory only");
  });
});
