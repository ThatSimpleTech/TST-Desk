// Instruction-stack store tests (TD-1201).

import { describe, expect, it } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, InstructionStack, InstructionStackEntry } from "./protocol";
import {
	cacheBadge,
	cacheLabel,
	clearStack,
	createStackState,
	createStackStore,
	formatTokens,
	memoryPlaceholderCopy,
	type StackState,
} from "./stack-store";

function entry(overrides: Partial<InstructionStackEntry> = {}): InstructionStackEntry {
	return {
		path: "/ws/AGENTS.md",
		precedence: "workspace",
		active: true,
		tokens: 120,
		token_method: "exact",
		warnings: [],
		is_fallback: false,
		...overrides,
	};
}

function stackEvent(overrides: Partial<InstructionStack> = {}): InstructionStack {
	return {
		type: "instruction_stack",
		seq: 1,
		session_id: "s1",
		sources: [entry()],
		total_tokens: 120,
		token_method: "exact",
		last_cached_tokens: null,
		...overrides,
	};
}

function harness(): { state: StackState; sent: ClientMessageUnion[]; store: ReturnType<typeof createStackStore> } {
	const state = createStackState();
	const sent: ClientMessageUnion[] = [];
	const store = createStackStore({ send: (msg) => (sent.push(msg), true) }, state);
	return { state, sent, store };
}

describe("applyEvent", () => {
	it("replaces state on instruction_stack for the attached session", () => {
		const { state, store } = harness();
		const event = stackEvent({ last_cached_tokens: 512 });
		expect(store.applyEvent(event, "s1")).toBe(true);
		expect(state.sessionId).toBe("s1");
		expect(state.sources).toEqual(event.sources);
		expect(state.totalTokens).toBe(120);
		expect(state.tokenMethod).toBe("exact");
		expect(state.lastCachedTokens).toBe(512);
		expect(state.loaded).toBe(true);
	});

	it("names loaded and dropped memory files", () => {
		const { state, store } = harness();
		store.applyEvent(
			stackEvent({
				memory: [{ path: ".tst/memory/MEMORY.md", tokens: 12, reason: "always-index" }],
				memory_dropped: [{ path: ".tst/memory/auth.md", tokens: 8, reason: "heading" }],
				memory_placeholder: false,
			}),
			"s1",
		);
		expect(state.memory.map((e) => e.path)).toEqual([".tst/memory/MEMORY.md"]);
		expect(state.memoryDropped.map((e) => e.reason)).toEqual(["heading"]);
		expect(state.memoryPlaceholder).toBe(false);
	});

	it("a second push replaces the first (hot reload live-update)", () => {
		const { state, store } = harness();
		store.applyEvent(stackEvent(), "s1");
		const reloaded = stackEvent({
			seq: 2,
			sources: [entry({ path: "/ws/nested/AGENTS.md", tokens: 40 })],
			total_tokens: 160,
		});
		expect(store.applyEvent(reloaded, "s1")).toBe(true);
		expect(state.sources.map((e) => e.path)).toEqual(["/ws/nested/AGENTS.md"]);
		expect(state.totalTokens).toBe(160);
	});

	it("ignores other event types", () => {
		const { state, store } = harness();
		const other = {
			type: "session_state",
			seq: 3,
			session_id: "s1",
			state: "idle",
		} as DaemonEventUnion;
		expect(store.applyEvent(other, "s1")).toBe(false);
		expect(state.loaded).toBe(false);
	});

	it("ignores stacks pushed for another session", () => {
		const { state, store } = harness();
		expect(store.applyEvent(stackEvent({ session_id: "s2" }), "s1")).toBe(false);
		expect(state.loaded).toBe(false);
	});

	it("a missing last_cached_tokens becomes null, not zero", () => {
		const { state, store } = harness();
		const event = stackEvent();
		delete event.last_cached_tokens;
		store.applyEvent(event, "s1");
		expect(state.lastCachedTokens).toBeNull();
	});

	// TD-1811: the two reasons last_cached_tokens is null, kept apart.
	it("carries cache_observed so silence is not read as a miss", () => {
		const { state, store } = harness();
		store.applyEvent(stackEvent({ cache_observed: true }), "s1");
		expect(state.cacheObserved).toBe(true);
		expect(state.lastCachedTokens).toBeNull();
	});

	it("a missing cache_observed becomes false, not true", () => {
		const { state, store } = harness();
		const event = stackEvent();
		delete event.cache_observed;
		store.applyEvent(event, "s1");
		expect(state.cacheObserved).toBe(false);
	});

	// TD-4502: skills ride the same event, listed apart from steering.
	it("carries loaded skills; an older daemon's omission reads as none", () => {
		const { state, store } = harness();
		store.applyEvent(
			stackEvent({
				skills_loaded: [{ name: "deploy", source: "workspace", tokens: 120 }],
			}),
			"s1",
		);
		expect(state.skillsLoaded.map((e) => e.name)).toEqual(["deploy"]);
		expect(state.skillsLoaded[0].tokens).toBe(120);

		const older = stackEvent({ seq: 2 });
		delete older.skills_loaded;
		store.applyEvent(older, "s1");
		expect(state.skillsLoaded).toEqual([]);
	});
});

describe("refresh", () => {
	it("sends get_instruction_stack for the attached session", () => {
		const { sent, store } = harness();
		expect(store.refresh("s1")).toBe(true);
		expect(sent).toEqual([{ type: "get_instruction_stack", session_id: "s1" }]);
	});

	it("with no session attached it clears state and sends nothing", () => {
		const { state, sent, store } = harness();
		store.applyEvent(stackEvent(), "s1");
		expect(store.refresh(null)).toBe(false);
		expect(sent).toEqual([]);
		expect(state.loaded).toBe(false);
		expect(state.sessionId).toBeNull();
	});

	it("drops the stale view when the session switched", () => {
		const { state, sent, store } = harness();
		store.applyEvent(stackEvent(), "s1");
		expect(store.refresh("s2")).toBe(true);
		expect(sent).toEqual([{ type: "get_instruction_stack", session_id: "s2" }]);
		expect(state.loaded).toBe(false);
		expect(state.sources).toEqual([]);
	});

	it("keeps the current view when re-asked for the same session", () => {
		const { state, store } = harness();
		store.applyEvent(stackEvent(), "s1");
		store.refresh("s1");
		expect(state.loaded).toBe(true);
		expect(state.sources).toHaveLength(1);
	});

	it("reports a dead socket as false", () => {
		const state = createStackState();
		const store = createStackStore({ send: () => false }, state);
		expect(store.refresh("s1")).toBe(false);
	});

	// TD-1204: the panel stays empty when a reply arrives with nobody
	// calling applyEvent. Refresh is the ask; applyEvent is the
	// subscribe. This test names the required order so a wiring race
	// cannot be "fixed" by dropping the send.
	it("a refresh reply only lands if applyEvent is already the subscriber", () => {
		const { state, sent, store } = harness();
		expect(store.refresh("s1")).toBe(true);
		expect(sent).toEqual([{ type: "get_instruction_stack", session_id: "s1" }]);
		expect(state.loaded).toBe(false);
		expect(store.applyEvent(stackEvent(), "s1")).toBe(true);
		expect(state.loaded).toBe(true);
		expect(state.sources).toHaveLength(1);
	});
});

describe("clear", () => {
	it("clears a populated state", () => {
		const state = createStackState();
		const store = createStackStore({ send: () => true }, state);
		store.applyEvent(stackEvent(), "s1");
		store.clear();
		expect(state).toEqual(createStackState());
	});

	it("clearStack matches createStackState", () => {
		const state = createStackState();
		state.sessionId = "s1";
		state.loaded = true;
		clearStack(state);
		expect(state).toEqual(createStackState());
	});
});

describe("labels", () => {
	it("formatTokens groups thousands", () => {
		expect(formatTokens(1200000)).toBe("1,200,000");
	});

	it("cacheLabel is honest before the first turn", () => {
		expect(cacheLabel(null, false)).toContain("unknown");
	});

	// TD-1811: a provider that sends no cached-token figure (Ollama sends
	// none) has not reported a miss, and must not be shown as one.
	it("cacheLabel says so when the provider reported no figure", () => {
		expect(cacheLabel(null, true)).toBe("provider reports no cache figure");
	});

	it("cacheLabel reports a miss only on a reported zero", () => {
		expect(cacheLabel(0, true)).toBe("cache miss");
	});

	it("cacheLabel reports a hit with the token count", () => {
		expect(cacheLabel(4200, true)).toBe("cached 4,200 tokens");
	});

	it("empty memory quotes the prompt placeholder", () => {
		expect(memoryPlaceholderCopy()).toBe("<!-- memory: none loaded for this session -->");
	});
});

describe("cacheBadge", () => {
	it("distinguishes all four states", () => {
		expect(cacheBadge(null, false)).toBe("unobserved");
		expect(cacheBadge(null, true)).toBe("unreported");
		expect(cacheBadge(0, true)).toBe("miss");
		expect(cacheBadge(4200, true)).toBe("hit");
	});

	it("an unreported figure is not styled as a miss", () => {
		expect(cacheBadge(null, true)).not.toBe("miss");
	});
});
