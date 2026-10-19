import { describe, expect, it } from "vitest";
import { hostCommandError, readTextFileCommand } from "./host-read";

describe("read_text_file command args", () => {
	it("sends the session id in camelCase so Tauri does not drop it", () => {
		expect(
			readTextFileCommand({
				path: "screens/aa.dataurl",
				workspace: null,
				sessionId: "s1",
			}),
		).toEqual({
			path: "screens/aa.dataurl",
			workspace: null,
			sessionId: "s1",
		});
		expect(
			readTextFileCommand({
				path: "screens/aa.dataurl",
				workspace: null,
				sessionId: "s1",
			}),
		).not.toHaveProperty("session_id");
	});
});

describe("hostCommandError", () => {
	it("keeps a string refusal from the shell", () => {
		expect(
			hostCommandError(
				"refused: no workspace or session dir to read under",
				"Could not read screen frame.",
			),
		).toBe("refused: no workspace or session dir to read under");
	});

	it("keeps an Error message and falls back otherwise", () => {
		expect(hostCommandError(new Error("too large to preview"), "fallback")).toBe("too large to preview");
		expect(hostCommandError(null, "fallback")).toBe("fallback");
	});
});
