// Command palette store (TD-1707).
//
// The palette's whole claim is that running an entry does the same thing the
// button does, so these tests mock only the connection seams (the way
// sessions.test.ts does) and let the real doctor, decisions, settings,
// sessions and right-pane stores load. Assertions are then on those stores'
// observable state — not on "the palette called the function I told it to".

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, SessionSummary } from "./protocol";

const mocks = vi.hoisted(() => {
	const eventHandlers = new Set<(e: DaemonEventUnion) => void>();
	return {
		eventHandlers,
		stateHandlers: new Set<(s: string) => void>(),
		sent: [] as ClientMessageUnion[],
		wsState: { state: "connected" },
		chatState: {
			sessionId: null as string | null,
			turnState: null as string | null,
			awaitingFirstToken: false,
		},
		statusState: { sessionId: null as string | null, workspacePath: null as string | null },
		chatSelects: [] as string[],
		inTauri: false,
		invoke: vi.fn(),
	};
});

vi.mock("@tauri-apps/api/core", () => ({
	invoke: (...args: unknown[]) => mocks.invoke(...args),
}));

vi.mock("./open-file", () => ({
	isTauri: () => mocks.inTauri,
	openInEditor: async () => false,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.eventHandlers.add(handler);
		return () => {
			mocks.eventHandlers.delete(handler);
		};
	},
	onConnectionState: (handler: (s: string) => void) => {
		mocks.stateHandlers.add(handler);
		return () => {
			mocks.stateHandlers.delete(handler);
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return true;
	},
	ws: mocks.wsState,
}));

vi.mock("./chat-store.svelte.js", () => ({
	chat: mocks.chatState,
	selectSession: (id: string) => {
		mocks.chatSelects.push(id);
		mocks.chatState.sessionId = id;
	},
}));

vi.mock("./session-status.svelte.js", () => ({
	focusSession: () => {},
	session: mocks.statusState,
	workspaceName: (p: string) =>
		p
			.split(/[\\/]/)
			.filter((s) => s.length > 0)
			.pop() ?? p,
}));

import { ICONS } from "./icons";
import {
	palette,
	openPalette,
	closePalette,
	setPaletteQuery,
	movePaletteSelection,
	runPaletteSelection,
	runPaletteEntry,
	paletteEntries,
	visibleEntries,
	resetPalette,
} from "./palette-store.svelte.js";
import { decisions, resetDecisions } from "./decisions.svelte.js";
import { doctor, resetDoctor } from "./doctor.svelte.js";
import { settings, resetSettings } from "./settings.svelte.js";
import { rightPane, resetRightPane } from "./right-pane.svelte.js";
import { sessions, startSessions, resetSessions } from "./sessions.svelte.js";
import { resetDesign } from "./design.svelte.js";

function sessionList(
	entries: Array<[id: string, updatedAt: string, state?: SessionSummary["state"]]>,
): DaemonEventUnion {
	return {
		type: "session_list",
		seq: 1,
		sessions: entries.map(([id, updatedAt, state]) => ({
			session_id: id,
			workspace_path: "/ws/proj",
			state: state ?? "idle",
			created_at: updatedAt,
			updated_at: updatedAt,
			event_count: 3,
			archived: false,
			starred: false,
		})),
	};
}

function emit(event: DaemonEventUnion): void {
	for (const h of mocks.eventHandlers) h(event);
}

/** Choose an entry by title, the way a user who typed it would. */
function run(title: string): void {
	setPaletteQuery(title);
	expect(visibleEntries()[0]?.title).toBe(title);
	runPaletteSelection();
}

beforeEach(() => {
	mocks.eventHandlers.clear();
	mocks.stateHandlers.clear();
	mocks.sent.length = 0;
	mocks.chatSelects.length = 0;
	mocks.inTauri = false;
	mocks.invoke.mockReset();
	mocks.chatState.sessionId = null;
	mocks.statusState.sessionId = null;
	mocks.statusState.workspacePath = null;
	resetPalette();
	resetDecisions();
	resetDoctor();
	resetSettings();
	resetRightPane();
	resetDesign();
	resetSessions();
	startSessions();
	mocks.sent.length = 0; // drop the start-time refresh from assertions
	openPalette();
});

describe("opening and dismissing", () => {
	it("opens clean — yesterday's query never comes back", () => {
		setPaletteQuery("doct");
		closePalette();
		expect(palette.open).toBe(false);
		openPalette();
		expect(palette.open).toBe(true);
		expect(palette.query).toBe("");
		expect(palette.index).toBe(0);
	});
});

describe("entries", () => {
	it("lists the actions and the daemon's sessions", () => {
		emit(sessionList([["s-newest", "2026-08-17T10:00:00Z"]]));
		const ids = paletteEntries().map((e) => e.id);
		expect(ids).toContain("action:new-session");
		expect(ids).toContain("action:open-decisions");
		expect(ids).toContain("action:run-doctor");
		expect(ids).toContain("action:show-stack");
		expect(ids).toContain("action:show-work");
		expect(ids).toContain("action:open-settings");
		expect(ids).toContain("action:toggle-theme");
		expect(ids).toContain("action:end-session");
		expect(ids).toContain("action:stop-computer-use");
		expect(ids).toContain("action:resume-computer-use");
		expect(ids).toContain("action:toggle-design");
		expect(ids).toContain("action:quit-app");
		expect(ids).toContain("session:s-newest");
	});

	it("invents no session the daemon didn't list", () => {
		expect(paletteEntries().filter((e) => e.id.startsWith("session:"))).toEqual([]);
		expect(sessions.rows).toEqual([]);
	});

	it("names only glyphs the shared icon map has, sessions included", () => {
		emit(sessionList([["s-1", "2026-08-17T10:00:00Z"]]));
		for (const entry of paletteEntries()) {
			expect(Object.keys(ICONS)).toContain(entry.icon);
		}
	});

	it("finds a session by its id", () => {
		emit(sessionList([["abcd1234ef", "2026-08-17T10:00:00Z"]]));
		setPaletteQuery("abcd1234");
		expect(visibleEntries()[0]?.id).toBe("session:abcd1234ef");
	});
});

describe("keyboard operation", () => {
	it("walks the list with ↑/↓ and wraps at both ends", () => {
		const count = visibleEntries().length;
		movePaletteSelection(1);
		expect(palette.index).toBe(1);
		movePaletteSelection(-1);
		expect(palette.index).toBe(0);
		movePaletteSelection(-1);
		expect(palette.index).toBe(count - 1);
		movePaletteSelection(1);
		expect(palette.index).toBe(0);
	});

	it("returns the highlight to the top when the query changes", () => {
		movePaletteSelection(2);
		expect(palette.index).toBe(2);
		setPaletteQuery("s");
		expect(palette.index).toBe(0);
	});

	it("stays open on Enter with nothing matched", () => {
		setPaletteQuery("qqqq");
		expect(visibleEntries()).toEqual([]);
		expect(runPaletteSelection()).toBe(false);
		expect(palette.open).toBe(true);
	});

	it("runs the highlighted entry, not the first one", () => {
		expect(visibleEntries()[0]?.title).toBe("New session");
		movePaletteSelection(1);
		expect(visibleEntries()[palette.index]?.title).toBe("Open decisions");
		expect(runPaletteSelection()).toBe(true);
		expect(decisions.open).toBe(true);
		// The entry at the top never ran — it would have sent new_session.
		expect(mocks.sent).toEqual([]);
	});
});

describe("commands", () => {
	it("opens the decisions ledger", () => {
		run("Open decisions");
		expect(decisions.open).toBe(true);
		expect(palette.open).toBe(false);
	});

	it("runs the doctor", () => {
		run("Run doctor");
		expect(doctor.open).toBe(true);
		expect(doctor.running).toBe(true);
		expect(mocks.sent).toContainEqual({ type: "run_diagnostics" });
	});

	it("opens the settings pane", () => {
		run("Open settings");
		expect(settings.open).toBe(true);
	});

	it("shows the stack panel", () => {
		expect(rightPane.tab).toBe("activity");
		run("Open stack");
		expect(rightPane.tab).toBe("stack");
	});

	it("shows the work pane", () => {
		expect(rightPane.tab).toBe("activity");
		run("Open work");
		expect(rightPane.tab).toBe("work");
	});

	it("toggles the theme, treating system as a way into dark", () => {
		expect(settings.theme).toBe("system");
		run("Toggle theme");
		expect(settings.theme).toBe("dark");
		openPalette();
		run("Toggle theme");
		expect(settings.theme).toBe("light");
	});

	it("asks the daemon for a new session in the attached workspace", () => {
		emit(sessionList([["s-1", "2026-08-17T10:00:00Z"]]));
		mocks.sent.length = 0;
		run("New session");
		expect(mocks.sent).toContainEqual({ type: "new_session", session_id: "s-1" });
	});

	it("attaches to a listed session", () => {
		emit(sessionList([["s-1", "2026-08-17T10:00:00Z"]]));
		run("Attach to s-1");
		expect(mocks.chatSelects).toEqual(["s-1"]);
		expect(palette.open).toBe(false);
	});

	it("dismisses even when the command could not act", () => {
		// No sessions listed and nothing attached: new-session has no anchor.
		runPaletteEntry(paletteEntries()[0]);
		expect(mocks.sent).toEqual([]);
		expect(palette.open).toBe(false);
	});

	it("stops computer use through the same message as the title bar", () => {
		run("Stop computer use");
		expect(mocks.sent).toEqual([{ type: "set_cu_kill", killed: true }]);
		expect(palette.open).toBe(false);
	});

	it("resumes computer use through the same message as the title bar", () => {
		run("Resume computer use");
		expect(mocks.sent).toEqual([{ type: "set_cu_kill", killed: false }]);
	});

	it("opens the Screen pane for Design mode", () => {
		expect(rightPane.tab).toBe("activity");
		run("Toggle Design mode");
		expect(rightPane.tab).toBe("screen");
	});

	it("asks the host to quit — close is not this", () => {
		mocks.inTauri = true;
		run("Quit TST Desk");
		expect(mocks.invoke).toHaveBeenCalledWith("quit_app");
		expect(palette.open).toBe(false);
	});
});
