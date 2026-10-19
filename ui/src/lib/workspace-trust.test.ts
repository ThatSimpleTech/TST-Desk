// Workspace config trust card model (TD-5113).

import { describe, expect, it } from "vitest";
import type { ConfigTrust } from "./protocol";
import { trustCardMode, trustMessage } from "./workspace-trust";

const loosened = [{ key: "boundary.network", value: "example.com", default: "deny" }];

function trust(state: ConfigTrust["state"], items = loosened): ConfigTrust {
  return { state, sha256: "b".repeat(64), loosened: items };
}

describe("trustCardMode", () => {
  it("asks while pending and offers a review after a decline", () => {
    expect(trustCardMode(trust("pending"))).toBe("ask");
    expect(trustCardMode(trust("declined"))).toBe("declined");
  });

  it("stays out of the way when the config is in force or there is nothing to ask", () => {
    expect(trustCardMode(trust("trusted"))).toBe("hidden");
    expect(trustCardMode(null)).toBe("hidden");
    expect(trustCardMode(undefined)).toBe("hidden");
    expect(trustCardMode(trust("pending", []))).toBe("hidden");
  });
});

describe("trustMessage", () => {
  it("names the hash the card showed, either way", () => {
    expect(trustMessage("s1", trust("pending"), false)).toEqual({
      type: "trust_workspace_config",
      session_id: "s1",
      sha256: "b".repeat(64),
      trust: false,
    });
  });
});
