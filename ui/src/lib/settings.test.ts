// @vitest-environment jsdom
//
// Tests for the settings store (TD-1703).
//
// jsdom because the appearance section stamps `data-theme` on <html> and
// reads localStorage. The store guards a missing store; the tests stub
// one so persistence is actually exercised (TD-1411).
//
// The store touches the daemon only through connection-status's
// sendToDaemon/onEvent, so the tests mock exactly that seam and drive the
// same setup_state / policy_rules events the daemon emits.
//
// Two things here are worth more than the rest: a tier that leaves its model
// to discovery must never render as an empty editable field, and the key
// section must not hold a credential anywhere in store state.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import type { ClientMessageUnion, DaemonEventUnion, SetupState } from "./protocol";

const mocks = vi.hoisted(() => ({
	handler: null as ((e: DaemonEventUnion) => void) | null,
	sent: [] as ClientMessageUnion[],
	sendOk: true,
}));

vi.mock("./connection-status.svelte.js", () => ({
	onEvent: (handler: (e: DaemonEventUnion) => void) => {
		mocks.handler = handler;
		return () => {
			mocks.handler = null;
		};
	},
	sendToDaemon: (msg: ClientMessageUnion) => {
		mocks.sent.push(msg);
		return mocks.sendOk;
	},
}));

import {
	settings,
	startSettings,
	resetSettings,
	openSettings,
	closeSettings,
	setSection,
	setTheme,
	isDiscovered,
	saveSlug,
	loadRules,
	revokeRule,
	setSkipAllApprovals,
	setLoadGlobalMemory,
} from "./settings.svelte.js";

function emit(event: DaemonEventUnion): void {
	mocks.handler?.(event);
}

// Node 26's experimental localStorage is off unless --localstorage-file
// is set; jsdom does not always install one either. Appearance is the
// only section that reads it, so the store already guards — the test
// has to supply a store if it wants to assert persistence (TD-1411).
const memory = new Map<string, string>();
const storageStub: Storage = {
	get length() {
		return memory.size;
	},
	clear: () => memory.clear(),
	getItem: (k: string) => memory.get(k) ?? null,
	key: (i: number) => [...memory.keys()][i] ?? null,
	removeItem: (k: string) => void memory.delete(k),
	setItem: (k: string, v: string) => void memory.set(k, v),
};

function setupState(over: Partial<SetupState> = {}): SetupState {
	return {
		type: "setup_state",
		seq: 1,
		has_api_key: true,
		key_required: true,
		presets: ["budget", "local", "tst-default"],
		active_preset: "local",
		tier_slugs: { brain: null, worker: null, validator: null },
		...over,
	};
}

beforeEach(() => {
	mocks.handler = null;
	mocks.sent = [];
	mocks.sendOk = true;
	memory.clear();
	vi.stubGlobal("localStorage", storageStub);
	resetSettings();
	document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe("opening", () => {
	it("opens on a section and closes", () => {
		startSettings();
		openSettings("model");
		expect(settings.open).toBe(true);
		expect(settings.section).toBe("model");
		setSection("policy");
		expect(settings.section).toBe("policy");
		closeSettings();
		expect(settings.open).toBe(false);
	});
});

describe("appearance", () => {
	it("stamps an explicit choice on the document", () => {
		startSettings();
		setTheme("dark");
		expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
		setTheme("light");
		expect(document.documentElement.getAttribute("data-theme")).toBe("light");
	});

	it("clears the attribute for system rather than resolving it", () => {
		// Resolving to light/dark here would freeze the choice at whatever the
		// OS was on startup; the media query re-answers it when the OS flips.
		startSettings();
		setTheme("dark");
		setTheme("system");
		expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
	});

	it("survives a restart", () => {
		const stop = startSettings();
		setTheme("dark");
		stop();
		resetSettings();
		startSettings();
		expect(settings.theme).toBe("dark");
		expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
	});

	it("falls back to system on a junk stored value", () => {
		localStorage.setItem("tst-desk:theme", "chartreuse");
		startSettings();
		expect(settings.theme).toBe("system");
	});

	it("falls back to system when there is no localStorage at all", () => {
		vi.stubGlobal("localStorage", undefined);
		startSettings();
		expect(settings.theme).toBe("system");
		setTheme("dark");
		expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
	});
});

describe("model section", () => {
	it("reads presets and slugs from setup_state", () => {
		startSettings();
		emit(setupState({ tier_slugs: { brain: "qwen3.8:27b", worker: null, validator: null } }));
		expect(settings.activePreset).toBe("local");
		expect(settings.presets).toContain("budget");
		expect(settings.tierSlugs.brain).toBe("qwen3.8:27b");
	});

	it("marks an unset tier as discovered, not as blank", () => {
		startSettings();
		emit(setupState());
		expect(isDiscovered("brain")).toBe(true);
		expect(settings.tierSlugs.brain).toBeNull();
	});

	it("invents nothing when the daemon omits the field", () => {
		startSettings();
		const { tier_slugs: _omitted, ...withoutSlugs } = setupState();
		emit(withoutSlugs as SetupState);
		expect(settings.tierSlugs).toEqual({});
	});

	it("sends set_tier_slug for the active preset", () => {
		startSettings();
		emit(setupState());
		saveSlug("brain", "qwen3.8:27b");
		expect(mocks.sent).toEqual([
			{ type: "set_tier_slug", preset: "local", tier: "brain", slug: "qwen3.8:27b" },
		]);
		expect(settings.savingTier).toBe("brain");
	});

	it("trims, and refuses a blank slug", () => {
		startSettings();
		emit(setupState());
		saveSlug("brain", "   ");
		expect(mocks.sent).toEqual([]);
		saveSlug("brain", "  qwen3.8:27b  ");
		expect(mocks.sent[0]).toMatchObject({ slug: "qwen3.8:27b" });
	});

	it("ends the in-flight save on the acking setup_state", () => {
		startSettings();
		emit(setupState());
		saveSlug("brain", "qwen3.8:27b");
		expect(settings.savingTier).toBe("brain");
		emit(setupState({ tier_slugs: { brain: "qwen3.8:27b", worker: null, validator: null } }));
		expect(settings.savingTier).toBeNull();
	});

	it("does not leave a save hanging when the socket is down", () => {
		startSettings();
		emit(setupState());
		mocks.sendOk = false;
		saveSlug("brain", "qwen3.8:27b");
		expect(settings.savingTier).toBeNull();
	});
});

describe("policy section", () => {
	it("asks for the attached session's rules", () => {
		startSettings();
		loadRules("s-1");
		expect(mocks.sent).toEqual([{ type: "list_policy_rules", session_id: "s-1" }]);
	});

	it("asks for nothing with no session, and holds no stale rules", () => {
		startSettings();
		loadRules("s-1");
		emit({ type: "policy_rules", seq: 2, rules: [{ tool: "fs_read", args: "**", effect: "auto" }] });
		expect(settings.rules).toHaveLength(1);

		mocks.sent = [];
		loadRules(null);
		expect(mocks.sent).toEqual([]);
		expect(settings.rules).toEqual([]);
	});

	it("revokes by tool and args, and takes the refreshed list from the daemon", () => {
		startSettings();
		loadRules("s-1");
		emit({ type: "policy_rules", seq: 2, rules: [{ tool: "fs_read", args: "**", effect: "auto" }] });
		mocks.sent = [];

		revokeRule("fs_read", "**");
		expect(mocks.sent).toEqual([
			{ type: "revoke_policy_rule", session_id: "s-1", tool: "fs_read", args: "**" },
		]);
		// Not spliced locally — the daemon's reply is what empties it.
		expect(settings.rules).toHaveLength(1);
		emit({ type: "policy_rules", seq: 3, rules: [] });
		expect(settings.rules).toEqual([]);
	});

	it("cannot revoke without a session", () => {
		startSettings();
		loadRules(null);
		revokeRule("fs_read", "**");
		expect(mocks.sent).toEqual([]);
	});

	it("reads skip-all from setup_state", () => {
		startSettings();
		emit(setupState({ skip_all_approvals: true }));
		expect(settings.skipAllApprovals).toBe(true);
	});

	it("treats an omitted skip-all field as off", () => {
		startSettings();
		emit(setupState({ skip_all_approvals: true }));
		const { skip_all_approvals: _omitted, ...without } = setupState();
		emit(without as SetupState);
		expect(settings.skipAllApprovals).toBe(false);
	});

	it("sends set_skip_all_approvals and waits for the ack", () => {
		startSettings();
		emit(setupState());
		setSkipAllApprovals(true);
		expect(mocks.sent).toEqual([{ type: "set_skip_all_approvals", enabled: true }]);
		// Not flipped locally — setup_state is the source of truth.
		expect(settings.skipAllApprovals).toBe(false);
		emit(setupState({ skip_all_approvals: true }));
		expect(settings.skipAllApprovals).toBe(true);
	});

	it("reads load-global-memory from setup_state and defaults off", () => {
		startSettings();
		expect(settings.loadGlobalMemory).toBe(false);
		emit(setupState({ load_global_memory: true }));
		expect(settings.loadGlobalMemory).toBe(true);
	});

	it("sends set_load_global_memory and waits for the ack", () => {
		startSettings();
		emit(setupState());
		setLoadGlobalMemory(true);
		expect(mocks.sent).toEqual([{ type: "set_load_global_memory", enabled: true }]);
		expect(settings.loadGlobalMemory).toBe(false);
		emit(setupState({ load_global_memory: true }));
		expect(settings.loadGlobalMemory).toBe(true);
	});
});

describe("key section", () => {
	it("reads key presence and requirement from setup_state", () => {
		startSettings();
		emit(setupState({ has_api_key: false, key_required: false }));
		expect(settings.hasApiKey).toBe(false);
		expect(settings.keyRequired).toBe(false);
	});

	it("holds no credential anywhere in store state", () => {
		// §2.2. The store carries presence, never the value — which is why
		// the daemon acks with setup_state instead of echoing the key.
		startSettings();
		emit(setupState({ has_api_key: true }));
		expect(JSON.stringify(settings)).not.toMatch(/sk-|api_key/);
	});
});
