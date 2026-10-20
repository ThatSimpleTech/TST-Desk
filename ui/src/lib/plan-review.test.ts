// TD-5123: the plan-review model and the `/plan` command, DOM-free.

import { describe, expect, it } from "vitest";
import fixtures from "./protocol-fixtures.json";
import { approveMessage, parsePlanCommand, planFromEvent, rejectMessage } from "./plan-review";
import type { PlanReview } from "./protocol";
import { BUILTIN_SLASH_ITEMS, filterCommands } from "./slash-commands";

const CARD = planFromEvent(fixtures.plan_review as PlanReview);

describe("review messages", () => {
	it("reads the daemon's plan_review", () => {
		expect(CARD).toEqual({ sessionId: "sess-1", toolCallId: "tc-p", plan: (fixtures.plan_review as PlanReview).plan });
	});

	it("approves the model's own plan when the editor is closed or unchanged", () => {
		for (const edited of [null, CARD.plan, `  ${CARD.plan}\n`]) {
			expect(approveMessage(CARD, edited, "")).toEqual({
				type: "review_plan",
				session_id: "sess-1",
				tool_call_id: "tc-p",
				decision: "approve",
				plan: null,
				feedback: "",
			});
		}
	});

	it("sends an edit as the plan to pin, with the note", () => {
		const msg = approveMessage(CARD, " 1. Only greet().\n", " keep it small ");
		expect([msg.decision, msg.plan, msg.feedback]).toEqual(["approve", "1. Only greet().", "keep it small"]);
	});

	it("rejects with the note and no plan", () => {
		expect(rejectMessage(CARD, " use the logger ")).toEqual({
			type: "review_plan",
			session_id: "sess-1",
			tool_call_id: "tc-p",
			decision: "reject",
			feedback: "use the logger",
		});
	});
});

describe("/plan", () => {
	it("toggles, sets, or plans a task", () => {
		expect(parsePlanCommand("/plan")).toEqual({ kind: "toggle" });
		expect(parsePlanCommand("  /plan  ")).toEqual({ kind: "toggle" });
		expect(parsePlanCommand("/plan on")).toEqual({ kind: "set", on: true });
		expect(parsePlanCommand("/PLAN Off")).toEqual({ kind: "set", on: false });
		expect(parsePlanCommand("/plan add a greeting\nto app.py")).toEqual({
			kind: "send",
			text: "add a greeting\nto app.py",
		});
	});

	it("leaves everything else to the model", () => {
		for (const draft of ["/planner", "plan this", "/compact", "/plans on", "please /plan"]) {
			expect(parsePlanCommand(draft)).toBeNull();
		}
	});

	it("is offered in the slash palette", () => {
		const [item] = filterCommands(BUILTIN_SLASH_ITEMS, "pl");
		expect(item?.name).toBe("plan");
		expect(item?.body).toBe("/plan ");
	});
});
