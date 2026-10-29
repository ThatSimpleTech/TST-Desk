// @vitest-environment jsdom
//
// The mascot (TD-5190): the drawing matches the logo pixel for pixel, the
// eyes move in whole pixels without touching the outline, and the chat pane
// shows it only on an empty chat, asleep while no session is live.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushSync, mount, unmount } from "svelte";
import type { DaemonEventUnion } from "./protocol";
import { MASCOT_HEIGHT, MASCOT_WIDTH, SHACKLE_PATH, bodyPath, bodyRows } from "./mascot";

const LOGO = [
	"........########........",
	"......############......",
	".....##############.....",
	".....####......####.....",
	"....####........####....",
	"....###..........###....",
	"....###..........###....",
	"....###..........###....",
	"....###..........###....",
	"........................",
	"..####################..",
	".######################.",
	"########################",
	"########################",
	"######..########..######",
	"#####....######....#####",
	"#####....######....#####",
	"#####....######....#####",
	"#####....######....#####",
	"######..########..######",
	"########################",
	"########################",
	"########################",
	".######################.",
	"..####################..",
];

/** Paint SVG path rectangles back onto a grid, to compare with the art. */
function paint(...paths: string[]): string[] {
	const grid = Array.from({ length: MASCOT_HEIGHT }, () => Array<string>(MASCOT_WIDTH).fill("."));
	for (const d of paths) {
		for (const [, x, y, w] of d.matchAll(/M(\d+) (\d+)h(\d+)v1h-\d+z/g)) {
			for (let i = 0; i < Number(w); i++) {
				const row = grid[Number(y)];
				if (row[Number(x) + i] === "#") throw new Error(`cell ${Number(x) + i},${y} painted twice`);
				row[Number(x) + i] = "#";
			}
		}
	}
	return grid.map((row) => row.join(""));
}

describe("the mascot drawing", () => {
	it("draws the logo: shackle, gap, body, eyes looking ahead", () => {
		expect(paint(SHACKLE_PATH, bodyPath({ gaze: "ahead", shut: false }))).toEqual(LOGO);
	});

	it("moves both eyes one whole pixel to glance, and nothing else", () => {
		const ahead = bodyRows({ gaze: "ahead", shut: false });
		const left = bodyRows({ gaze: "left", shut: false });
		const right = bodyRows({ gaze: "right", shut: false });
		expect(left[5]).toBe("####....######....######");
		expect(right[5]).toBe("######....######....####");
		expect(left[4]).toBe("#####..########..#######");
		expect(right[4]).toBe("#######..########..#####");
		// Rows without eyes are identical in every frame.
		for (const rows of [left, right]) {
			rows.forEach((row, i) => {
				if (i < 4 || i > 9) expect(row).toBe(ahead[i]);
			});
		}
	});

	it("blinks to a one-pixel slit per eye", () => {
		const full = "########################";
		expect(bodyRows({ gaze: "ahead", shut: true }).slice(4, 10)).toEqual([
			full,
			full,
			full,
			"#####....######....#####",
			full,
			full,
		]);
	});
});

const mocks = vi.hoisted(() => ({
	handlers: new Set<(event: DaemonEventUnion) => void>(),
}));

vi.mock("./connection-status.svelte.js", async (importOriginal) => {
	const real = await importOriginal<typeof import("./connection-status.svelte.js")>();
	return {
		...real,
		onDaemonEvent: (handler: (event: DaemonEventUnion) => void) => {
			mocks.handlers.add(handler);
			return () => mocks.handlers.delete(handler);
		},
		sendToDaemon: () => true,
		attachToSession: () => {},
		detachFromSession: () => {},
	};
});

import ChatPane from "./components/chat/ChatPane.svelte";
import { ws } from "./connection-status.svelte.js";

let app: ReturnType<typeof mount> | null = null;

function emit(event: DaemonEventUnion): void {
	for (const handler of [...mocks.handlers]) handler(event);
	flushSync();
}

function mascot(): HTMLElement | null {
	return document.querySelector<HTMLElement>("[data-testid='mascot']");
}

beforeEach(() => {
	mocks.handlers.clear();
	const data = new Map<string, string>();
	const storage: Storage = {
		getItem: (key) => data.get(key) ?? null,
		setItem: (key, value) => void data.set(key, value),
		removeItem: (key) => void data.delete(key),
		clear: () => data.clear(),
		key: (index) => [...data.keys()][index] ?? null,
		get length() {
			return data.size;
		},
	};
	Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
	if (typeof globalThis.ResizeObserver === "undefined") {
		globalThis.ResizeObserver = class {
			observe(): void {}
			unobserve(): void {}
			disconnect(): void {}
		} as unknown as typeof ResizeObserver;
	}
	ws.state = "connected";
});

afterEach(() => {
	if (app !== null) {
		unmount(app);
		app = null;
	}
	ws.state = "disconnected";
});

describe("the mascot on a new chat", () => {
	it("sleeps with no live session, wakes when one binds, and leaves once the chat starts", () => {
		app = mount(ChatPane, { target: document.body });
		flushSync();
		expect(mascot()?.dataset.state).toBe("asleep");
		expect(mascot()?.getAttribute("aria-hidden")).toBe("true");

		emit({
			type: "session_list",
			seq: 1,
			sessions: [
				{
					session_id: "s1",
					workspace_path: "/workspace",
					state: "idle",
					created_at: "2026-10-29T10:00:00Z",
					updated_at: "2026-10-29T10:00:00Z",
					event_count: 0,
					archived: false,
					starred: false,
				},
			],
		});
		expect(mascot()?.dataset.state).toBe("awake");
		expect(mascot()?.querySelectorAll("path.frame")).toHaveLength(4);

		emit({ type: "user_turn", session_id: "s1", turn_id: "t1", content: "hello", seq: 2 });
		expect(mascot()).toBeNull();
	});
});
