import { describe, expect, it } from "vitest";
import {
	BROWSER_TOOLS,
	SCREEN_EMPTY_COPY,
	isBrowserTool,
	screenPreviewSidecar,
	screenTabVisible,
} from "./screen";

describe("screen helpers (TD-1710)", () => {
	it("names the six browser verbs", () => {
		expect([...BROWSER_TOOLS].sort()).toEqual([
			"browser_click",
			"browser_navigate",
			"browser_screenshot",
			"browser_scroll",
			"browser_type",
			"browser_wait",
		]);
		expect(isBrowserTool("browser_click")).toBe(true);
		expect(isBrowserTool("desktop_click")).toBe(false);
		expect(isBrowserTool("fs_read")).toBe(false);
	});

	it("empty copy points at the first computer-use turn", () => {
		expect(SCREEN_EMPTY_COPY).toMatch(/first computer-use turn/i);
	});

	it("shows the tab once this session has a frame or a browser tool", () => {
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s1",
				hasFrame: true,
				hasBrowserTool: false,
			}),
		).toBe(true);
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s1",
				hasFrame: false,
				hasBrowserTool: true,
			}),
		).toBe(true);
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s1",
				hasFrame: false,
				hasBrowserTool: false,
			}),
		).toBe(false);
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s2",
				hasFrame: true,
				hasBrowserTool: true,
			}),
		).toBe(false);
		expect(
			screenTabVisible({
				boundSessionId: null,
				sessionId: "s1",
				hasFrame: true,
				hasBrowserTool: true,
			}),
		).toBe(false);
	});

	it("maps a png path to the text sidecar the host can read", () => {
		expect(screenPreviewSidecar("screens/ab.png")).toBe("screens/ab.dataurl");
	});
});
