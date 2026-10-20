// @vitest-environment jsdom
//
// TD-5124: the done-check on an interactive turn, in the window. The daemon
// runs the check and sends `stop_check`; the chat shows it on the reply it
// judged, folded like a tool call, and opens a failure the turn ended on.
// A retry's feedback went to the model only, so it is never a user bubble.
// `/goal` shows as a strip above the composer until a check meets it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { DaemonEventUnion, StopCheck } from "./protocol";
import fixtures from "./protocol-fixtures.json";

const mocks = vi.hoisted(() => ({
  handlers: [] as ((e: DaemonEventUnion) => void)[],
}));

vi.mock("./connection-status.svelte.js", () => ({
  onEvent: (handler: (e: DaemonEventUnion) => void) => {
    mocks.handlers.push(handler);
    return () => {
      mocks.handlers = mocks.handlers.filter((h) => h !== handler);
    };
  },
  sendToDaemon: () => true,
}));

import CheckFold from "./components/chat/CheckFold.svelte";
import GoalStrip from "./components/chat/GoalStrip.svelte";
import { createChatState, createChatStore, type ChatDeps } from "./chat-store";
import { turnFailureCopy } from "./error-copy";
import { goalFor, resetGoals, startGoals } from "./goal-store.svelte.js";
import { checkBody, checkFromEvent, checkLabel, checkOpenByDefault } from "./stop-check";

let seq = 1;

function ev(event: Record<string, unknown>): DaemonEventUnion {
  seq += 1;
  return { session_id: "s1", seq, ...event } as DaemonEventUnion;
}

const delta = (text: string): DaemonEventUnion => ev({ type: "assistant_delta", delta: text });
const turnComplete = (code: string | null = null): DaemonEventUnion =>
  ev({
    type: "turn_complete",
    tokens: 0,
    cost: 0,
    tier: "brain",
    duration: 1,
    failed: code !== null,
    error_code: code,
  });

function check(outcome: StopCheck["outcome"], attempt = 1, passed = false): StopCheck {
  return {
    type: "stop_check",
    session_id: "s1",
    seq: ++seq,
    outcome,
    attempt,
    max_retries: 3,
    items: [
      {
        item: "$ npm test",
        source: "workspace",
        via: "shell",
        passed,
        decided: outcome !== "unchecked",
        detail: "exit code: 1\nFAILED date.test.ts",
      },
    ],
  };
}

function bound() {
  const deps: ChatDeps = { send: () => true, attach: () => {}, detach: () => {} };
  const state = createChatState();
  const store = createChatStore(deps, state);
  store.applyEvent({
    type: "session_list",
    seq: 1,
    sessions: [
      {
        session_id: "s1",
        workspace_path: "/workspace",
        state: "idle",
        created_at: "2026-10-20T10:00:00Z",
        updated_at: "2026-10-20T10:00:00Z",
        event_count: 0,
        archived: false,
        starred: false,
      },
    ],
  });
  store.applyEvent(ev({ type: "user_turn", turn_id: "t1", content: "fix it" }));
  const apply = (...events: DaemonEventUnion[]) => events.forEach((e) => store.applyEvent(e));
  return { state, apply };
}

describe("the done-check on the reply", () => {
  it("lands on the reply it judged, and the retry streams after it", () => {
    const { state, apply } = bound();
    apply(delta("All done."), check("retry", 1), delta("Fixed the date test."), check("passed", 2, true));
    apply(turnComplete());

    expect(state.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    const reply = state.messages[1];
    expect(reply.parts?.map((p) => p.kind)).toEqual(["text", "check", "text", "check"]);
    const checks = (reply.parts ?? []).flatMap((p) => (p.kind === "check" ? [p.check] : []));
    expect(checks.map((c) => c.outcome)).toEqual(["retry", "passed"]);
    expect(reply.text).toBe("All done.Fixed the date test.");
    expect(state.lastTurnError).toBeNull();
  });

  it("ends a turn on the failure and keeps it in the reply", () => {
    const { state, apply } = bound();
    apply(delta("Done."), check("failed", 4), turnComplete("stop_check_failed"));
    const parts = state.messages[1].parts ?? [];
    expect(parts[parts.length - 1]).toMatchObject({ kind: "check", check: { outcome: "failed" } });
    expect(state.lastTurnError).toBe("stop_check_failed");
  });

  it("ignores another session's check", () => {
    const { state, apply } = bound();
    apply(delta("Done."), { ...check("retry"), session_id: "other" });
    expect(state.messages[1].parts?.map((p) => p.kind)).toEqual(["text"]);
  });
});

describe("wording", () => {
  it("says what happened, one line", () => {
    expect(checkLabel(checkFromEvent(check("passed", 1, true)))).toBe("Done-check passed");
    expect(checkLabel(checkFromEvent(check("retry", 2)))).toBe(
      "Done-check failed · try 2 of 3, the agent keeps working",
    );
    expect(checkLabel(checkFromEvent(check("failed", 4)))).toBe("Done-check still failing after 3 tries");
    expect(checkLabel({ ...checkFromEvent(check("failed")), maxRetries: 0 })).toBe("Done-check failed");
    expect(checkLabel(checkFromEvent(check("unchecked")))).toBe("Done-check could not run");
  });

  it("lists each item with its mark, and a failure's output under it", () => {
    const fixture = checkFromEvent(fixtures.stop_check as unknown as StopCheck);
    expect(checkBody(fixture)).toBe(
      [
        "✗ $ uv run pytest -q",
        "  exit code: 1",
        "  ── stdout ──",
        "  FAILED tests/test_dates.py::test_leap_day",
        "✓ Goal: The parser handles leap days",
      ].join("\n"),
    );
    expect(checkBody(checkFromEvent(check("unchecked")))).toMatch(/^\? \$ npm test/);
  });

  it("opens a failure the turn ended on, not a retry or a pass", () => {
    expect(checkOpenByDefault(checkFromEvent(check("failed")))).toBe(true);
    expect(checkOpenByDefault(checkFromEvent(check("unchecked")))).toBe(true);
    expect(checkOpenByDefault(checkFromEvent(check("retry")))).toBe(false);
    expect(checkOpenByDefault(checkFromEvent(check("passed", 1, true)))).toBe(false);
  });

  it("has its own copy for the failed turn", () => {
    expect(turnFailureCopy("stop_check_failed")?.title).toBe("Done-check still failing");
  });
});

describe("components", () => {
  let target: HTMLElement;
  let component: ReturnType<typeof mount> | null = null;
  let stop: () => void = () => {};

  beforeEach(() => {
    resetGoals();
    stop = startGoals();
    target = document.createElement("div");
    document.body.appendChild(target);
  });

  afterEach(() => {
    if (component !== null) unmount(component);
    component = null;
    stop();
    target.remove();
  });

  it("shows a failed check open, with what failed", () => {
    component = mount(CheckFold, { target, props: { check: checkFromEvent(check("failed", 4)) } });
    flushSync();
    expect(target.textContent).toContain("Done-check still failing after 3 tries");
    expect(target.textContent).toContain("FAILED date.test.ts");
    const button = target.querySelector("button");
    expect(button?.getAttribute("aria-expanded")).toBe("true");
    button?.click();
    flushSync();
    expect(target.textContent).not.toContain("FAILED date.test.ts");
  });

  it("folds a passing check", () => {
    component = mount(CheckFold, { target, props: { check: checkFromEvent(check("passed", 1, true)) } });
    flushSync();
    expect(target.textContent).toContain("Done-check passed");
    expect(target.querySelector("button")?.getAttribute("aria-expanded")).toBe("false");
  });

  it("keeps each session's goal from the daemon's events", () => {
    const emit = (event: DaemonEventUnion) => {
      for (const handler of mocks.handlers) handler(event);
      flushSync();
    };
    emit(fixtures.session_goal as unknown as DaemonEventUnion);
    expect(goalFor("sess-1")).toBe("The parser handles leap days");
    expect(goalFor("other")).toBeNull();
    emit({ type: "session_goal", session_id: "sess-1", goal: null, change: "met", seq: 30 } as DaemonEventUnion);
    expect(goalFor("sess-1")).toBeNull();
  });

  it("shows the goal strip only while there is a goal", () => {
    component = mount(GoalStrip, { target, props: { goal: "The parser handles leap days" } });
    flushSync();
    const strip = target.querySelector('[aria-label="Session goal"]');
    expect(strip?.textContent).toContain("The parser handles leap days");
    expect(target.querySelector("button")).toBeNull();
    unmount(component);
    component = mount(GoalStrip, { target, props: { goal: null } });
    flushSync();
    expect(target.querySelector('[aria-label="Session goal"]')).toBeNull();
  });
});
