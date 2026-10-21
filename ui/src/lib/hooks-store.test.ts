// Hooks inspector store tests (TD-5134).

import { describe, expect, it } from "vitest";
import {
	createHooksState,
	createHooksStore,
	hookEventLabel,
	lastRunLabel,
	workspaceNote,
} from "./hooks-store";
import type { ClientMessageUnion, HookInfo, HookRun, HooksState } from "./protocol";

function hook(overrides: Partial<HookInfo> = {}): HookInfo {
	return {
		id: "user:before_tool:0",
		source: "user",
		event: "before_tool",
		command: "~/bin/guard.sh",
		timeout_seconds: 30,
		tools: ["shell"],
		last: null,
		...overrides,
	};
}

function reply(overrides: Partial<HooksState> = {}): HooksState {
	return { type: "hooks_state", seq: 1, session_id: "s1", hooks: [hook()], ...overrides };
}

function run(overrides: Partial<HookRun> = {}): HookRun {
	return {
		type: "hook_run",
		seq: 9,
		session_id: "s1",
		hook_id: "user:before_tool:0",
		source: "user",
		event: "before_tool",
		command: "~/bin/guard.sh",
		result: "block",
		exit_code: 2,
		reason: "no force pushes",
		duration_ms: 12,
		...overrides,
	};
}

function setup() {
	const sent: ClientMessageUnion[] = [];
	const state = createHooksState();
	const store = createHooksStore({ send: (m) => (sent.push(m), true) }, state);
	return { sent, state, store };
}

describe("hooks store", () => {
	it("asks for the attached session's hooks", () => {
		const { sent, store } = setup();
		expect(store.refresh("s1")).toBe(true);
		expect(sent).toEqual([{ type: "get_hooks", session_id: "s1" }]);
	});

	it("lists the active hooks from the reply, only for the attached session", () => {
		const { state, store } = setup();
		expect(store.applyEvent(reply({ session_id: "other" }), "s1")).toBe(false);
		expect(state.loaded).toBe(false);
		expect(store.applyEvent(reply(), "s1")).toBe(true);
		expect(state.hooks.map((h) => h.id)).toEqual(["user:before_tool:0"]);
	});

	it("a live run becomes the hook's last result", () => {
		const { state, store } = setup();
		store.applyEvent(reply(), "s1");
		expect(store.applyEvent(run(), "s1")).toBe(true);
		expect(state.hooks[0].last?.result).toBe("block");
		expect(lastRunLabel(state.hooks[0].last)).toBe("blocked · 12 ms");
		expect(store.applyEvent(run({ hook_id: "user:stop:3" }), "s1")).toBe(false);
	});

	it("drops another session's view on refresh", () => {
		const { state, store } = setup();
		store.applyEvent(reply(), "s1");
		store.refresh("s2");
		expect(state.loaded).toBe(false);
		expect(state.hooks).toEqual([]);
	});

	it("says why workspace hooks are missing", () => {
		const { state, store } = setup();
		store.applyEvent(reply({ workspace_state: "pending" }), "s1");
		expect(workspaceNote(state)).toMatch(/not trusted yet/);
		store.applyEvent(reply({ workspace_error: "Invalid hooks" }), "s1");
		expect(workspaceNote(state)).toMatch(/Invalid hooks/);
	});

	it("labels", () => {
		expect(hookEventLabel("stop")).toBe("Turn end");
		expect(lastRunLabel(null)).toBe("not run yet");
		expect(lastRunLabel(run({ result: "error", exit_code: 1 }))).toBe("error (exit 1) · 12 ms");
	});
});
