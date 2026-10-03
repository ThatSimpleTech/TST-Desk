import { describe, expect, it } from "vitest";
import { resumePausedSession } from "./resume-paused";

describe("resumePausedSession", () => {
	it("sends resume only while the session is paused", () => {
		const sent: unknown[] = [];
		const send = (msg: unknown) => {
			sent.push(msg);
			return true;
		};
		expect(resumePausedSession("s1", "paused", send)).toBe(true);
		expect(resumePausedSession("s1", "running", send)).toBe(false);
		expect(resumePausedSession(null, "paused", send)).toBe(false);
		expect(sent).toEqual([{ type: "resume", session_id: "s1" }]);
	});
});
