import { describe, expect, it } from "vitest";
import { isHandshakeRefusal } from "./connection-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";

const refusal = {
  type: "error",
  code: "auth_failed",
  message: "Invalid auth token. Check the token in the port file and try again.",
} as DaemonEventUnion;

describe("isHandshakeRefusal (TD-5005)", () => {
  it("is the daemon refusing the hello before the socket is connected", () => {
    expect(isHandshakeRefusal(refusal, "connecting")).toBe(true);
    expect(isHandshakeRefusal(refusal, "reconnecting")).toBe(true);
  });

  it("is not a provider auth error on a session", () => {
    const provider = { ...refusal, session_id: "s1" } as DaemonEventUnion;
    expect(isHandshakeRefusal(provider, "connecting")).toBe(false);
  });

  it("is not anything once the handshake succeeded", () => {
    expect(isHandshakeRefusal(refusal, "connected")).toBe(false);
  });

  it("is not another error code", () => {
    const other = { ...refusal, code: "bad_request" } as DaemonEventUnion;
    expect(isHandshakeRefusal(other, "connecting")).toBe(false);
  });
});
