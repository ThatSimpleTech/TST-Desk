// TD-5268: a child's approval card says which child is asking, and goes
// when the call that started the child returns.

import { describe, expect, it } from "vitest";

import { approvalFromEvent, settledBy, workerLine } from "./approval";
import type { ApprovalRequest, ApprovalWorker } from "./protocol";

function event(worker?: Partial<ApprovalWorker> | null): ApprovalRequest {
	return {
		type: "approval_request",
		seq: 7,
		session_id: "s1",
		tool_call_id: worker ? "call_wf.2/call_0" : "call_own",
		tool_name: "shell",
		arguments: { command: "make test" },
		decision_class: "B",
		summary: "Run `make test`",
		reason: "class B",
		...(worker === undefined
			? {}
			: {
					worker:
						worker === null
							? null
							: { id: "call_wf.2", parent_call_id: "call_wf", via: "workflow", kind: "worker", ...worker },
				}),
	} as ApprovalRequest;
}

describe("workerLine", () => {
	it("is empty for the session's own agent, and for an older daemon's event", () => {
		expect(workerLine(approvalFromEvent(event()))).toBe("");
		expect(workerLine(approvalFromEvent(event(null)))).toBe("");
		expect(approvalFromEvent(event()).worker).toBeNull();
	});

	it("says which child, on what", () => {
		expect(workerLine(approvalFromEvent(event({ index: 2, label: "API module" })))).toBe(
			"A worker is asking, on: API module.",
		);
		expect(workerLine(approvalFromEvent(event({ kind: "research", label: "Read the docs" })))).toBe(
			"A research worker is asking, on: Read the docs.",
		);
		expect(workerLine(approvalFromEvent(event({ kind: "check", index: 2 })))).toBe(
			"A check is asking.",
		);
		expect(workerLine(approvalFromEvent(event({ agent: "db-migrator", label: "Migrate" })))).toBe(
			'The worker "db-migrator" is asking, on: Migrate.',
		);
	});

	it("says when the paths are in the child's own copy", () => {
		const line = workerLine(
			approvalFromEvent(event({ label: "Refactor", worktree_branch: "tst/wt/abc" })),
		);
		expect(line).toContain("a copy of its own (tst/wt/abc)");
		expect(line).toContain("the paths here are in that copy");
	});
});

describe("settledBy", () => {
	it("is the call itself for the session's own card", () => {
		const own = approvalFromEvent(event());
		expect(settledBy(own, "call_own")).toBe(true);
		expect(settledBy(own, "call_wf")).toBe(false);
	});

	it("is also the delegate or workflow call for a child's card", () => {
		const child = approvalFromEvent(event({}));
		expect(settledBy(child, "call_wf.2/call_0")).toBe(true);
		expect(settledBy(child, "call_wf")).toBe(true);
		expect(settledBy(child, "call_other")).toBe(false);
	});
});
