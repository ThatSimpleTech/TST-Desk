// Tests for the session state store (TD-1006).
//
// The store is protocol-shaped, so these tests drive it with the same
// events the daemon emits: session_state adoption, tier_state following,
// cost accumulation with the per-tier breakdown, boundary updates, and
// the picker/set-tier actions it sends back.

import { describe, it, expect, beforeEach } from "vitest";
import { DEFAULT_ATTACHMENT_LIMITS } from "./attachments";
import type { ProtocolClient } from "./client";
import {
  session,
  bindClient,
  ingestEvent,
  liveModelLabel,
  openWorkspace,
  resetSession,
  setPlan,
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
  setPlan: (id: string, on: boolean) => sent.push(`plan:${id}:${on}`),
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
      preset: "vllm",
      hosts: { brain: "openrouter.ai", worker: "127.0.0.1:8002", validator: "openrouter.ai" },
      seq: 2,
    } as DaemonEventUnion);

    expect(session.tier).toBe("worker");
    expect(session.tierOverride).toBeNull();
    expect(session.modelSlugs.worker).toBe("w-slug");
    expect(session.preset).toBe("vllm");
    expect(session.hosts.worker).toBe("127.0.0.1:8002");

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

  it("follows plan from tier_state and sends set_plan", () => {
    ingestEvent(sessionState("s1"));
    expect(session.planMode).toBe(false);

    ingestEvent({
      type: "tier_state",
      session_id: "s1",
      tier: "brain",
      override: null,
      model_slugs: { brain: "b-slug" },
      plan: true,
      seq: 2,
    } as DaemonEventUnion);

    expect(session.planMode).toBe(true);
    expect(session.tier).toBe("brain");

    setPlan(false);
    expect(sent).toEqual(["plan:s1:false"]);
    // The store does not guess — plan stays on until the daemon acks.
    expect(session.planMode).toBe(true);

    ingestEvent({
      type: "tier_state",
      session_id: "s1",
      tier: "brain",
      override: null,
      model_slugs: { brain: "b-slug" },
      plan: false,
      seq: 3,
    } as DaemonEventUnion);
    expect(session.planMode).toBe(false);
  });

  it("does not locally change tier when a chip is clicked during plan", () => {
    ingestEvent(sessionState("s1"));
    ingestEvent({
      type: "tier_state",
      session_id: "s1",
      tier: "brain",
      override: null,
      model_slugs: { brain: "b-slug", worker: "w-slug" },
      plan: true,
      seq: 2,
    } as DaemonEventUnion);

    setTier("worker");
    expect(sent).toEqual(["tier:s1:worker"]);
    expect(session.tier).toBe("brain");
    expect(session.planMode).toBe(true);
  });

  it("treats an omitted plan field as off", () => {
    ingestEvent(sessionState("s1"));
    ingestEvent({
      type: "tier_state",
      session_id: "s1",
      tier: "worker",
      override: null,
      model_slugs: { worker: "w-slug" },
      seq: 2,
    } as DaemonEventUnion);
    expect(session.planMode).toBe(false);
  });

  it("keeps hosts empty when an older daemon omits them", () => {
    ingestEvent(sessionState("s1"));
    ingestEvent({
      type: "tier_state",
      session_id: "s1",
      tier: "brain",
      override: null,
      model_slugs: { brain: "b-slug" },
      seq: 2,
    } as DaemonEventUnion);
    expect(session.preset).toBe("");
    expect(session.hosts).toEqual({});
  });
});

describe("liveModelLabel", () => {
  it("joins slug and host for the active tier", () => {
    expect(
      liveModelLabel("brain", { brain: "stealth/ox-alpha" }, { brain: "openrouter.ai" }),
    ).toBe("stealth/ox-alpha · openrouter.ai");
  });

  it("shows the host alone when the slug is still unresolved", () => {
    expect(liveModelLabel("brain", {}, { brain: "127.0.0.1:8002" })).toBe("127.0.0.1:8002");
  });

  it("does not invent a host from the slug", () => {
    expect(liveModelLabel("brain", { brain: "stealth/ox-alpha" }, {})).toBe("stealth/ox-alpha");
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

// ── Attachment caps (TD-1709) ─────────────────────────────────────────

describe("attachment limits", () => {
  it("starts on the shipped defaults so the composer can judge a drop", () => {
    resetSession();
    expect(session.attachmentLimits).toEqual(DEFAULT_ATTACHMENT_LIMITS);
  });

  it("takes the workspace's caps off boundary_update", () => {
    resetSession();
    ingestEvent({ type: "session_state", session_id: "s1", state: "idle", seq: 1 });
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
      attachments: { max_file_bytes: 1024, max_total_bytes: 2048, max_count: 2 },
    });
    expect(session.attachmentLimits).toEqual({
      max_file_bytes: 1024,
      max_total_bytes: 2048,
      max_count: 2,
    });
  });

  it("keeps the last known caps when a daemon omits them", () => {
    // An older daemon sends no `attachments`; standing the composer's early
    // refusal down to nothing would be worse than a slightly stale number.
    resetSession();
    ingestEvent({ type: "session_state", session_id: "s1", state: "idle", seq: 1 });
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
    });
    expect(session.attachmentLimits).toEqual(DEFAULT_ATTACHMENT_LIMITS);
  });
});
