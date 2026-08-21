import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
}));

import {
	resetScreen,
	screen,
	setScreenPreviewReader,
	startScreen,
} from "./screen.svelte.js";
import { screenTabVisible } from "./screen";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

describe("screen store (TD-1710, TD-3401)", () => {
	beforeEach(() => {
		resetScreen();
		startScreen();
	});

	afterEach(() => {
		resetScreen();
	});

	it("hides the tab until the first CU tool this session", () => {
		expect(
			screenTabVisible({
				boundSessionId: screen.boundSessionId,
				sessionId: "s1",
				hasFrame: screen.hasFrame,
				hasCuTool: screen.hasCuTool,
			}),
		).toBe(false);
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "desktop_screenshot",
			arguments: {},
		});
		expect(screen.hasCuTool).toBe(true);
		expect(screen.boundSessionId).toBe("s1");
		expect(screen.hasFrame).toBe(false);
		expect(
			screenTabVisible({
				boundSessionId: screen.boundSessionId,
				sessionId: "s1",
				hasFrame: screen.hasFrame,
				hasCuTool: screen.hasCuTool,
			}),
		).toBe(true);
	});

	it("shows after a browser tool_call and keeps the bound session", () => {
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "browser_navigate",
			arguments: { url: "https://example.com" },
		});
		expect(screen.hasCuTool).toBe(true);
		expect(screen.boundSessionId).toBe("s1");
		expect(screen.hasFrame).toBe(false);
	});

	it("ignores a filesystem tool_result", () => {
		emit({
			type: "tool_call",
			seq: 1,
			session_id: "s1",
			tool_call_id: "c1",
			name: "fs_read",
			arguments: { path: "a.md" },
		});
		emit({
			type: "tool_result",
			seq: 2,
			session_id: "s1",
			tool_call_id: "c1",
			status: "success",
			output: "ok",
			truncated: false,
		});
		expect(screen.hasCuTool).toBe(false);
	});

	it("loads a path-only screen_frame through the sidecar reader", async () => {
		setScreenPreviewReader(async ({ path, sessionId }) => {
			expect(path).toBe("screens/aa.dataurl");
			expect(sessionId).toBe("s1");
			return "data:image/png;base64,abc";
		});
		emit({
			type: "screen_frame",
			seq: 3,
			session_id: "s1",
			path: "screens/aa.png",
			mime: "image/png",
			width: 1,
			height: 1,
			tool_call_id: "c2",
		});
		expect(screen.hasFrame).toBe(true);
		expect(screen.path).toBe("screens/aa.png");
		await vi.waitFor(() => {
			expect(screen.preview).toBe("data:image/png;base64,abc");
		});
		expect(screen.error).toBeNull();
	});
});
