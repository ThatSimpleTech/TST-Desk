// Tests for rail turn-activity (TD-1720).
//
// "running" is loop liveness, not a turn. The rail paints working / waiting
// / finished from busy + parked states, and the bound pane's turn evidence
// overlays the attached row.

import { describe, expect, it } from "vitest";
import {
	ACTIVITY_LABELS,
	ROW_TITLE_MAX_LEN,
	activityTone,
	rowActivity,
	rowTitle,
	rowTitleFull,
	type ActivityRow,
} from "./rail-activity";

function row(over: Partial<ActivityRow> = {}): ActivityRow {
	return {
		sessionId: "abc12345-0000-0000-0000-000000000000",
		state: "running",
		busy: false,
		title: null,
		...over,
	};
}

describe("rowActivity", () => {
	it("does not treat running as working", () => {
		expect(rowActivity(row({ state: "running", busy: false }))).toBe("finished");
		expect(ACTIVITY_LABELS.finished).toBe("Finished");
		expect(activityTone("finished")).toBe("muted");
	});

	it("busy on a live session is working", () => {
		expect(rowActivity(row({ state: "running", busy: true }))).toBe("working");
		expect(activityTone("working")).toBe("info");
	});

	it("awaiting approval is waiting even when busy is unset", () => {
		expect(rowActivity(row({ state: "awaiting_approval", busy: false }))).toBe("waiting");
		expect(activityTone("waiting")).toBe("warning");
	});

	it("paused and failed keep their own tones", () => {
		expect(rowActivity(row({ state: "paused" }))).toBe("paused");
		expect(activityTone("paused")).toBe("warning");
		expect(rowActivity(row({ state: "failed" }))).toBe("failed");
		expect(activityTone("failed")).toBe("danger");
	});

	it("a terminal session is finished even if busy was left on", () => {
		expect(rowActivity(row({ state: "complete", busy: true }))).toBe("finished");
		expect(rowActivity(row({ state: "cancelled", busy: true }))).toBe("finished");
	});

	it("the bound pane's turn evidence overlays the attached row", () => {
		const attached = row({ state: "running", busy: false });
		const bound = { sessionId: attached.sessionId, turnState: "running" as const, awaitingFirstToken: false };
		expect(rowActivity(attached, bound)).toBe("working");
		expect(
			rowActivity(attached, { ...bound, turnState: "awaiting_approval" }),
		).toBe("waiting");
		expect(rowActivity(attached, { ...bound, turnState: null })).toBe("finished");
		expect(
			rowActivity(attached, { sessionId: attached.sessionId, turnState: null, awaitingFirstToken: true }),
		).toBe("working");
	});

	it("bound overlay does not leak onto another row", () => {
		const other = row({ sessionId: "other", state: "running", busy: false });
		expect(
			rowActivity(other, {
				sessionId: "abc12345-0000-0000-0000-000000000000",
				turnState: "running",
				awaitingFirstToken: false,
			}),
		).toBe("finished");
	});
});

describe("rowTitle cap", () => {
	it("caps display titles at 20 characters", () => {
		expect(ROW_TITLE_MAX_LEN).toBe(20);
		const long = row({ title: "abcdefghijklmnopqrstuvwxyz" });
		expect(rowTitleFull(long)).toBe("abcdefghijklmnopqrstuvwxyz");
		expect(rowTitle(long)).toBe("abcdefghijklmnopqrstuvwxyz".slice(0, 20) + "…");
		expect(rowTitle(long).length).toBe(21);
	});

	it("leaves short titles and the short-id fallback alone", () => {
		expect(rowTitle(row({ title: "Fix the rail titles" }))).toBe("Fix the rail titles");
		expect(rowTitle(row({ title: null }))).toBe("abc12345");
		expect(rowTitleFull(row({ title: "  Named  " }))).toBe("Named");
	});
});
