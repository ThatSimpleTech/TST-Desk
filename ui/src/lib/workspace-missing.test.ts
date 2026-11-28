// ERRORS-02: a chat whose folder is gone gets a standing banner, not a
// six-second toast, and the daemon's sentence (which names the folder and
// says the message was not sent) is shown as it is.

import { describe, expect, it } from "vitest";
import { daemonErrorCopy } from "./error-copy";

describe("workspace_missing", () => {
  it("is a banner that keeps the daemon's words", () => {
    const message = "This chat's folder is gone: /work/proj. The message was not sent.";
    const copy = daemonErrorCopy("workspace_missing", message);
    expect(copy.severity).toBe("banner");
    expect(copy.title).toBe("This chat's folder is gone");
    expect(copy.body).toBe(message);
  });
});
