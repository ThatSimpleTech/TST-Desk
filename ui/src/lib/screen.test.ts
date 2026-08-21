import { describe, expect, it } from "vitest";
import {
	BROWSER_TOOLS,
	DESKTOP_TOOLS,
	SCREEN_EMPTY_COPY,
	isBrowserTool,
	isCuTool,
	screenPreviewSidecar,
	screenTabVisible,
} from "./screen";

describe("screen helpers (TD-1710, TD-3401)", () => {
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

	it("treats browser and desktop verbs as computer-use", () => {
		expect([...DESKTOP_TOOLS].sort()).toEqual([
			"desktop_click",
			"desktop_move",
			"desktop_screenshot",
			"desktop_scroll",
			"desktop_type",
		]);
		expect(isCuTool("browser_screenshot")).toBe(true);
		expect(isCuTool("desktop_screenshot")).toBe(true);
		expect(isCuTool("desktop_click")).toBe(true);
		expect(isCuTool("fs_read")).toBe(false);
	});

	it("empty copy points at the first computer-use turn", () => {
		expect(SCREEN_EMPTY_COPY).toMatch(/first computer-use turn/i);
		expect(SCREEN_EMPTY_COPY).toMatch(/fills/i);
	});

	it("hides the tab until this session has a frame or a CU tool", () => {
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s1",
				hasFrame: true,
				hasCuTool: false,
			}),
		).toBe(true);
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s1",
				hasFrame: false,
				hasCuTool: true,
			}),
		).toBe(true);
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s1",
				hasFrame: false,
				hasCuTool: false,
			}),
		).toBe(false);
		expect(
			screenTabVisible({
				boundSessionId: "s1",
				sessionId: "s2",
				hasFrame: true,
				hasCuTool: true,
			}),
		).toBe(false);
		expect(
			screenTabVisible({
				boundSessionId: null,
				sessionId: "s1",
				hasFrame: true,
				hasCuTool: true,
			}),
		).toBe(false);
	});

	it("maps a png path to the text sidecar the host can read", () => {
		expect(screenPreviewSidecar("screens/ab.png")).toBe("screens/ab.dataurl");
	});
});
