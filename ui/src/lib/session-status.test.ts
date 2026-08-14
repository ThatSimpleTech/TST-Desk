// Tests for the session state store (TD-1006).
//
// The store is protocol-shaped, so these tests drive it with the same
// events the daemon emits: session_state adoption, tier_state following,
// cost accumulation with the per-tier breakdown, boundary updates, and
// the picker/set-tier actions it sends back.

import { describe, it, expect, beforeEach } from "vitest";
import type { ProtocolClient } from "./client";
import {
  session,
  bindClient,
  ingestEvent,
  openWorkspace,
  resetSession,
  setTier,
  workspaceName,
} from "./session-status.svelte.js";
import type { DaemonEventUnion } from "./protocol";

let sent: string[];
let attached: string[];

// Minimal ProtocolClient stand-in — records what the store asks it to send.
const fakeClient = {
  openWorkspace: (path: string) => sent.push(`open:${path}`),
  setTier: (id: string, tier: string) => sent.push(`tier:${id}:${tier}`),
  attach: (id: string) => attached.push(id),
} as unknown as ProtocolClient;

function sessionState(sessionId: string, state = "running"): DaemonEventUnion {
  return {
    type: "session_state",
    session_id: sessionId,
    state: state as "running",
    seq: 1,
  } as DaemonEventUnion;
}

beforeEach(() => {
  resetSession();
  sent = [];
  attached = [];
  bindClient(fakeClient);
});

describe("session adoption", () => {
  it("adopts the first session_state and auto-attaches", () => {
    openWorkspace("/Users/me/project");
    ingestEvent(sessionState("s1"));

    expect(session.sessionId).toBe("s1");
    // The workspace name comes from the path we asked to open — events
    // don't echo it.
    expect(session.workspacePath).toBe("/Users/me/project");
    expect(session.state).toBe("running");
    expect(attached).toEqual(["s1"]);
  });

  it("ignores events from other sessions after adoption", () => {
    ingestEvent(sessionState("s1"));
    ingestEvent(sessionState("s2", "failed"));

    expect(session.sessionId).toBe("s1");
    expect(session.state).toBe("running");
  });

  it("opens nothing without a client", () => {
    bindClient(null);
    openWorkspace("/x");
    expect(sent).toEqual([]);
  });
});

describe("tier state", () => {
  it("follows tier_state and sends set_tier", () => {
    ingestEvent(sessionState("s1"));
    ingestEvent({
      type: "tier_state",
      session_id: "s1",
      tier: "worker",
      override: null,
      model_slugs: { brain: "b-slug", worker: "w-slug", validator: "v-slug" },
      seq: 2,
    } as DaemonEventUnion);

    expect(session.tier).toBe("worker");
    expect(session.tierOverride).toBeNull();
    expect(session.modelSlugs.worker).toBe("w-slug");

    setTier("validator");
    expect(sent).toEqual(["tier:s1:validator"]);

    ingestEvent({
      type: "tier_state",
      session_id: "s1",
      tier: "validator",
      override: "validator",
      model_slugs: { brain: "b-slug", worker: "w-slug", validator: "v-slug" },
      seq: 3,
    } as DaemonEventUnion);
    expect(session.tier).toBe("validator");
    expect(session.tierOverride).toBe("validator");
  });
});

describe("cost meter", () => {
  it("tracks live cost with a per-tier breakdown", () => {
    ingestEvent(sessionState("s1"));
    ingestEvent({
      type: "cost_update",
      session_id: "s1",
      turn_cost: 0.02,
      session_cost: 0.02,
      total_cost: 0.02,
      classifier_cost: 0,
      cost_by_tier: { brain: 0.02 },
      seq: 2,
    } as DaemonEventUnion);
    ingestEvent({
      type: "cost_update",
      session_id: "s1",
      turn_cost: 0.02,
      session_cost: 0.0305,
      total_cost: 0.0305,
      classifier_cost: 0.0005,
      cost_by_tier: { brain: 0.0305 },
      seq: 3,
    } as DaemonEventUnion);

    expect(session.cost.session).toBeCloseTo(0.0305);
    expect(session.cost.byTier.brain).toBeCloseTo(0.0305);
    expect(session.cost.classifier).toBeCloseTo(0.0005);
  });
});

describe("boundary (wall)", () => {
  it("stores the boundary from boundary_update", () => {
    ingestEvent(sessionState("s1"));
    ingestEvent({
      type: "boundary_update",
      session_id: "s1",
      writable_paths: ["**"],
      allowed_commands: ["git"],
      network: "deny",
      spend_usd: 25,
      wall_clock_hours: 8,
      max_iterations: 200,
      source: "defaults",
      seq: 2,
    } as DaemonEventUnion);

    expect(session.boundary?.spend_usd).toBe(25);
    expect(session.boundary?.allowed_commands).toEqual(["git"]);
  });
});

describe("workspaceName", () => {
  it("takes the basename across separators", () => {
    expect(workspaceName("/Users/me/project")).toBe("project");
    expect(workspaceName("C:\\work\\repo")).toBe("repo");
    expect(workspaceName("/")).toBe("/");
  });
});
