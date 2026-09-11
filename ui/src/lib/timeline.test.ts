// Activity-timeline store tests (TD-1005).

import { describe, it, expect } from "vitest";
import { Timeline, eventToEntry, summarizeArguments, type TimelineEntry } from "./timeline";
import type { DaemonEventUnion } from "./protocol";

function evt(e: DaemonEventUnion): DaemonEventUnion {
  return e;
}

/** A timeline showing session "s1" — the session every fixture below names.
 *  An unbound timeline shows nothing at all (TD-1009). */
function bound(sessionId = "s1"): Timeline {
  const t = new Timeline();
  t.bind(sessionId);
  return t;
}

describe("eventToEntry", () => {
  it("maps a tool_call to a tool_call entry with arguments + class", () => {
    const entry = eventToEntry(
      evt({
        type: "tool_call",
        session_id: "s1",
        tool_call_id: "tc1",
        name: "fs_write",
        arguments: { path: "a.txt", content: "hi" },
        decision_class: "B",
        seq: 4,
      }),
    );
    expect(entry).not.toBeNull();
    expect(entry!.kind).toBe("tool_call");
    expect(entry!.title).toBe("fs_write");
    expect(entry!.toolCallId).toBe("tc1");
    expect(entry!.details.decision_class).toBe("B");
  });

  it("maps a failed desktop click to a tool_result row, not a screen pane (TD-3401)", () => {
    const entry = eventToEntry(
      evt({
        type: "tool_result",
        session_id: "s1",
        tool_call_id: "c1",
        status: "error",
        output: "expected the foreground window to match 'Chrome'",
        truncated: false,
        error_code: "focus_mismatch",
        seq: 4,
      }),
    );
    expect(entry).not.toBeNull();
    expect(entry!.kind).toBe("tool_result");
    expect(entry!.title).toBe("Error");
    expect(entry!.details.error_code).toBe("focus_mismatch");
  });

  it("maps a tool_result and carries the diff for file writes", () => {
    const entry = eventToEntry(
      evt({
        type: "tool_result",
        session_id: "s1",
        tool_call_id: "tc1",
        status: "success",
        output: "wrote 3 bytes",
        truncated: false,
        diff: "--- a/a.txt\n+++ b/a.txt\n",
        seq: 5,
      }),
    );
    expect(entry!.kind).toBe("tool_result");
    expect(entry!.details.diff).toBe("--- a/a.txt\n+++ b/a.txt\n");
  });

  it("maps an approval_request to an approval entry and a denied result to 'Denied'", () => {
    const approval = eventToEntry(
      evt({
        type: "approval_request",
        session_id: "s1",
        tool_call_id: "tc1",
        tool_name: "fs_write",
        arguments: { path: "a.txt" },
        decision_class: "C",
        summary: "Write a.txt",
        reason: "decision class C requires approval",
        seq: 5,
      }),
    );
    expect(approval!.kind).toBe("approval");
    expect(approval!.title).toBe("Write a.txt");
    expect(approval!.toolCallId).toBe("tc1");
    expect(approval!.details.status).toBe("pending");

    const denied = eventToEntry(
      evt({
        type: "tool_result",
        session_id: "s1",
        tool_call_id: "tc1",
        status: "error",
        output: "Denied by user: not safe",
        truncated: false,
        error_code: "approval_denied",
        seq: 6,
      }),
    );
    expect(denied!.title).toBe("Denied");
    expect(denied!.details.error_code).toBe("approval_denied");
  });

  it("maps a verify_result to a timeline row, not a chat bubble", () => {
    const entry = eventToEntry(
      evt({
        type: "verify_result",
        session_id: "s1",
        verdict: "pass",
        summary: "Diff matches the tests.",
        cost: 0.002,
        pending: false,
        seq: 11,
      }),
    );
    expect(entry).not.toBeNull();
    expect(entry!.kind).toBe("verify");
    expect(entry!.title).toBe("Verify pass");
    expect(entry!.preview).toBe("Diff matches the tests.");
    expect(entry!.details.pending).toBe(false);

    const pending = eventToEntry(
      evt({
        type: "verify_result",
        session_id: "s1",
        verdict: "error",
        summary: "Confirm to review this write.",
        cost: 0,
        pending: true,
        seq: 12,
      }),
    );
    expect(pending!.title).toBe("Verify pending");
    expect(pending!.details.pending).toBe(true);
  });

  it("maps a decision_logged to a decision entry with class + rule", () => {
    const entry = eventToEntry(
      evt({
        type: "decision_logged",
        session_id: "s1",
        decision_class: "A",
        what: "Formatted file",
        why: "ruff format",
        commit: "abc123",
        seq: 9,
      }),
    );
    expect(entry!.kind).toBe("decision");
    expect(entry!.title).toBe("Class A");
    expect(entry!.details.why).toBe("ruff format");
  });

  it("maps tier_switched, context_compacted, steering_reloaded, and error", () => {
    const tier = eventToEntry(
      evt({ type: "tier_switched", session_id: "s1", tier: "worker", previous: "brain", seq: 10 }),
    );
    expect(tier!.kind).toBe("tier_switch");
    expect(tier!.title).toBe("brain → worker");

    const compact = eventToEntry(
      evt({
        type: "context_compacted",
        session_id: "s1",
        dropped_messages: 12,
        kept_messages: 8,
        tokens_before: 12000,
        tokens_after: 4000,
        seq: 11,
      }),
    );
    expect(compact!.kind).toBe("compaction");

    const reload = eventToEntry(
      evt({
        type: "steering_reloaded",
        session_id: "s1",
        prefix_hash: "abc",
        prefix_tokens: 100,
        steering_tokens: 60,
        source_count: 3,
        seq: 12,
      }),
    );
    expect(reload!.kind).toBe("steering_reload");
    // Both figures reach the inspector: the prefix is what gets re-billed,
    // steering_tokens is what the user's own files cost (TD-1810).
    expect(reload!.details.prefix_tokens).toBe(100);
    expect(reload!.details.steering_tokens).toBe(60);

    const activated = eventToEntry(
      evt({ type: "rule_activated", session_id: "s1", rule_path: ".tst/rules/api.md", seq: 13 }),
    );
    expect(activated!.kind).toBe("steering_reload");
    expect(activated!.title).toBe("Rule activated");
    expect(activated!.preview).toBe(".tst/rules/api.md");

    const err = eventToEntry(evt({ type: "error", code: "test", message: "boom", seq: 14 }));
    expect(err!.kind).toBe("error");
    expect(err!.title).toBe("test");
  });

  it("returns null for events that are not activity entries", () => {
    for (const e of [
      evt({ type: "assistant_delta", session_id: "s1", delta: "hi", seq: 1 }),
      evt({ type: "session_state", session_id: "s1", state: "running", seq: 1 }),
      evt({ type: "turn_complete", session_id: "s1", tokens: 1, cost: 0, tier: "worker", duration: 1, failed: false, error_code: null, seq: 1 }),
      // Turn boundaries are the Timeline's to fold, not a per-event map.
      evt({ type: "user_turn", session_id: "s1", turn_id: "t1", content: "hi", seq: 1 }),
      evt({
        type: "shell_output",
        session_id: "s1",
        tool_call_id: "tc1",
        stream: "stdout",
        chunk: "x",
        seq: 1,
      }),
    ]) {
      expect(eventToEntry(e)).toBeNull();
    }
  });
});

describe("summarizeArguments", () => {
  it("summarizes a single argument", () => {
    expect(summarizeArguments({ command: "cargo test" })).toBe('command="cargo test"');
  });

  it("handles no arguments", () => {
    expect(summarizeArguments({})).toBe("(no arguments)");
  });
});

describe("Timeline", () => {
  it("accumulates entries in seq order and skips non-entries", () => {
    const t = bound();
    t.push(evt({ type: "assistant_delta", session_id: "s1", delta: "hi", seq: 1 }));
    t.push(
      evt({
        type: "tool_call",
        session_id: "s1",
        tool_call_id: "tc1",
        name: "shell",
        arguments: { command: "ls" },
        seq: 2,
      }),
    );
    t.push(
      evt({
        type: "tool_result",
        session_id: "s1",
        tool_call_id: "tc1",
        status: "success",
        output: "a.txt",
        truncated: false,
        seq: 3,
      }),
    );

    expect(t.length).toBe(2);
    expect(t.entries.map((e) => e.kind)).toEqual(["tool_call", "tool_result"]);
    expect(t.entries.map((e) => e.seq)).toEqual([2, 3]);
  });

  it("merges shell_output chunks into the parent tool_call live buffer", () => {
    const t = bound();
    t.push(
      evt({
        type: "tool_call",
        session_id: "s1",
        tool_call_id: "tc1",
        name: "shell",
        arguments: { command: "ls" },
        seq: 2,
      }),
    );
    t.push(evt({ type: "shell_output", session_id: "s1", tool_call_id: "tc1", stream: "stdout", chunk: "a.txt\n", seq: 3 }));
    t.push(evt({ type: "shell_output", session_id: "s1", tool_call_id: "tc1", stream: "stderr", chunk: "warn\n", seq: 4 }));
    t.push(evt({ type: "shell_output", session_id: "s1", tool_call_id: "tc1", stream: "stdout", chunk: "b.txt\n", seq: 5 }));

    const entry = t.entries[0] as TimelineEntry;
    expect(entry.stdout).toBe("a.txt\nb.txt\n");
    expect(entry.stderr).toBe("warn\n");
    // Streaming chunks do not create new entries.
    expect(t.length).toBe(1);
  });

  it("drops an orphan shell_output chunk with no parent tool_call", () => {
    const t = bound();
    t.push(evt({ type: "shell_output", session_id: "s1", tool_call_id: "tc9", stream: "stdout", chunk: "orphan", seq: 1 }));
    expect(t.length).toBe(0);
  });

  it("flips an approval entry to denied when its tool_result resolves", () => {
    const t = bound();
    t.push(
      evt({
        type: "approval_request",
        session_id: "s1",
        tool_call_id: "tc1",
        tool_name: "shell",
        arguments: { command: "rm -rf /" },
        decision_class: "C",
        summary: "Run `rm -rf /`",
        reason: "decision class C requires approval",
        seq: 2,
      }),
    );
    expect(t.entries[0].details.status).toBe("pending");

    t.push(
      evt({
        type: "tool_result",
        session_id: "s1",
        tool_call_id: "tc1",
        status: "error",
        output: "Denied by user",
        truncated: false,
        error_code: "approval_denied",
        seq: 3,
      }),
    );
    expect(t.entries[0].details.status).toBe("denied");
  });

  it("marks an approval entry approved on a successful result", () => {
    const t = bound();
    t.push(
      evt({
        type: "approval_request",
        session_id: "s1",
        tool_call_id: "tc1",
        tool_name: "shell",
        arguments: { command: "ls" },
        decision_class: "B",
        summary: "Run `ls`",
        reason: "decision class B requires approval",
        seq: 2,
      }),
    );
    t.push(
      evt({
        type: "tool_result",
        session_id: "s1",
        tool_call_id: "tc1",
        status: "success",
        output: "a.txt",
        truncated: false,
        seq: 3,
      }),
    );
    expect(t.entries[0].details.status).toBe("approved");
  });

  it("leaves an approval entry pending when an unrelated tool_result lands", () => {
    const t = bound();
    t.push(
      evt({
        type: "approval_request",
        session_id: "s1",
        tool_call_id: "tc1",
        tool_name: "shell",
        arguments: { command: "ls" },
        decision_class: "B",
        summary: "Run `ls`",
        reason: "decision class B requires approval",
        seq: 2,
      }),
    );
    t.push(
      evt({
        type: "tool_result",
        session_id: "s1",
        tool_call_id: "tc-other",
        status: "success",
        output: "ok",
        truncated: false,
        seq: 3,
      }),
    );
    expect(t.entries[0].details.status).toBe("pending");
  });

  it("clear resets the list", () => {
    const t = bound();
    t.push(
      evt({
        type: "tool_call",
        session_id: "s1",
        tool_call_id: "tc1",
        name: "fs_read",
        arguments: { path: "x" },
        seq: 2,
      }),
    );
    expect(t.length).toBe(1);
    t.clear();
    expect(t.length).toBe(0);
  });
});

describe("Timeline turns", () => {
  const userTurn = (seq: number, content = "Review the auth middleware") =>
    evt({ type: "user_turn", session_id: "s1", turn_id: `t${seq}`, content, seq });
  const toolCall = (seq: number) =>
    evt({
      type: "tool_call",
      session_id: "s1",
      tool_call_id: `tc${seq}`,
      name: "fs_read",
      arguments: { path: "a.txt" },
      seq,
    });
  const complete = (seq: number, failed = false) =>
    evt({
      type: "turn_complete",
      session_id: "s1",
      tokens: 1204,
      cost: 0.03,
      tier: "brain",
      duration: 12.4,
      failed,
      error_code: failed ? "provider_error" : null,
      seq,
    });

  it("opens a numbered header on each user_turn, in order", () => {
    const t = bound();
    t.push(userTurn(1));
    t.push(toolCall(2));
    t.push(userTurn(3, "  Now   fix\nit "));
    expect(t.entries.map((e) => e.kind)).toEqual(["turn", "tool_call", "turn"]);
    expect(t.entries[0].title).toBe("Turn 1");
    expect(t.entries[0].preview).toBe("Review the auth middleware");
    expect(t.entries[0].details.status).toBe("running");
    expect(t.entries[2].title).toBe("Turn 2");
    // Whitespace collapses so the header stays one line.
    expect(t.entries[2].preview).toBe("Now fix it");
  });

  it("closes the open turn with the daemon's duration and cost, adding no row", () => {
    const t = bound();
    t.push(userTurn(1));
    t.push(toolCall(2));
    t.push(complete(3));
    expect(t.length).toBe(2);
    const turn = t.entries[0];
    expect(turn.details.status).toBe("complete");
    expect(turn.details.duration).toBe(12.4);
    expect(turn.details.cost).toBe(0.03);
    expect(turn.details.tokens).toBe(1204);
    expect(turn.details.tier).toBe("brain");
  });

  it("marks a failed turn and keeps the error code", () => {
    const t = bound();
    t.push(userTurn(1));
    t.push(complete(2, true));
    expect(t.entries[0].details.status).toBe("failed");
    expect(t.entries[0].details.error_code).toBe("provider_error");
  });

  it("settles a still-running turn when the session stops", () => {
    const t = bound();
    t.push(userTurn(1));
    t.push(evt({ type: "session_state", session_id: "s1", state: "cancelled", seq: 2 }));
    expect(t.entries[0].details.status).toBe("cancelled");
  });

  it("leaves a closed turn alone when a later session_state lands", () => {
    const t = bound();
    t.push(userTurn(1));
    t.push(complete(2));
    t.push(evt({ type: "session_state", session_id: "s1", state: "idle", seq: 3 }));
    t.push(evt({ type: "session_state", session_id: "s1", state: "cancelled", seq: 4 }));
    expect(t.entries[0].details.status).toBe("complete");
  });

  it("annotates only the most recent turn", () => {
    const t = bound();
    t.push(userTurn(1));
    t.push(complete(2));
    t.push(userTurn(3));
    t.push(complete(4, true));
    expect(t.entries[0].details.status).toBe("complete");
    expect(t.entries[1].details.status).toBe("failed");
  });

  it("drops a turn_complete with no header to annotate", () => {
    const t = bound();
    t.push(complete(1));
    expect(t.length).toBe(0);
  });

  it("restarts numbering after clear", () => {
    const t = bound();
    t.push(userTurn(1));
    t.push(userTurn(2));
    t.clear();
    t.push(userTurn(3));
    expect(t.entries[0].title).toBe("Turn 1");
  });
});
