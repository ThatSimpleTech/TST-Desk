import { describe, it, expect } from "vitest";
import { pickQuickEntrySession } from "./quick-entry";
import type { SessionSummary } from "./protocol";

function summary(
	id: string,
	path: string,
	updated: string,
	state: SessionSummary["state"] = "idle",
): SessionSummary {
	return {
		session_id: id,
		workspace_path: path,
		state,
		created_at: updated,
		updated_at: updated,
		event_count: 1,
		archived: false,
		starred: false,
	};
}

describe("pickQuickEntrySession", () => {
	it("refuses when there is no last workspace path", () => {
		expect(pickQuickEntrySession([], null)).toEqual({
			action: "none",
			reason: "no_path",
		});
	});

	it("attaches to the newest live session for the path", () => {
		const pick = pickQuickEntrySession(
			[
				summary("s1", "/a", "2026-08-27T10:00:00Z"),
				summary("s2", "/a", "2026-08-27T11:00:00Z"),
				summary("s3", "/b", "2026-08-27T12:00:00Z"),
			],
			"/a",
		);
		expect(pick).toEqual({ action: "attach", sessionId: "s2" });
	});

	it("opens the workspace when no live session matches", () => {
		expect(
			pickQuickEntrySession(
				[summary("s1", "/b", "2026-08-27T10:00:00Z", "complete")],
				"/a",
			),
		).toEqual({ action: "open" });
	});

	it("ignores terminal sessions for the target path", () => {
		expect(
			pickQuickEntrySession(
				[summary("s1", "/a", "2026-08-27T10:00:00Z", "failed")],
				"/a",
			),
		).toEqual({ action: "open" });
	});
});
