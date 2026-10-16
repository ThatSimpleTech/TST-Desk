import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DaemonEventUnion } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	invoke: vi.fn(async () => "data:image/png;base64,from-host"),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));

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

	it("asks the host for the sidecar with a camelCase session id", async () => {
		(globalThis as { window?: unknown }).window = { __TAURI_INTERNALS__: {} };
		mocks.invoke.mockClear();
		try {
			emit({
				type: "screen_frame",
				seq: 5,
				session_id: "s1",
				path: "screens/aa.png",
				mime: "image/png",
				width: 768,
				height: 427,
				tool_call_id: "c4",
			});
			await vi.waitFor(() => {
				expect(screen.preview).toBe("data:image/png;base64,from-host");
			});
			expect(mocks.invoke).toHaveBeenCalledWith("read_text_file", {
				path: "screens/aa.dataurl",
				workspace: null,
				sessionId: "s1",
			});
		} finally {
			delete (globalThis as { window?: unknown }).window;
		}
	});

	it("shows a string refusal from the host", async () => {
		setScreenPreviewReader(async () => {
			throw "refused: no workspace or session dir to read under";
		});
		emit({
			type: "screen_frame",
			seq: 4,
			session_id: "s1",
			path: "screens/aa.png",
			mime: "image/png",
			width: 768,
			height: 427,
			tool_call_id: "c3",
		});
		await vi.waitFor(() => {
			expect(screen.error).toBe("refused: no workspace or session dir to read under");
		});
		expect(screen.preview).toBeNull();
	});
});
