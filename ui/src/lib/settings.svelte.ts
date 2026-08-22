// Settings store (TD-1703).
//
// The title-bar gear opens this, not the wizard — the wizard stays for first
// run only. Everything shown comes from the daemon's `setup_state` and
// `policy_rules`; nothing is inferred locally (AGENTS §6).
//
// The key section reuses TD-1102's flows from the onboarding store rather
// than repeating them. A credential should have one code path, not two.
//
// Wiring mirrors doctor.svelte.ts: listens on the connection fan-out, sends
// only via sendToDaemon — no client reference, no import cycle.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, McpServerInfo, PolicyRuleSummary } from "./protocol";

export type SettingsSection = "appearance" | "model" | "policy" | "key" | "mcp";
export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
	"appearance",
	"model",
	"policy",
	"mcp",
	"key",
] as const;

export type Theme = "light" | "system" | "dark";
export const THEMES: readonly Theme[] = ["light", "system", "dark"] as const;

const THEME_KEY = "tst-desk:theme";

export const settings = $state({
	open: false,
	section: "appearance" as SettingsSection,
	theme: "system" as Theme,
	/** Model section, from setup_state. */
	presets: [] as string[],
	activePreset: null as string | null,
	/** Slug per tier as the *config file* has it; null = left to discovery. */
	tierSlugs: {} as Record<string, string | null>,
	/** Tier whose save is in flight; cleared by the acking setup_state. */
	savingTier: null as string | null,
	/** Key section, from setup_state. */
	hasApiKey: false,
	keyRequired: true,
	/** Policy section. Rules are per-workspace, so they need a session. */
	rules: [] as PolicyRuleSummary[],
	rulesSessionId: null as string | null,
	/** Machine-wide skip-all (TD-804). From setup_state, not inferred. */
	skipAllApprovals: false,
	/** Machine-wide global memory (TD-2603). From setup_state. */
	loadGlobalMemory: false,
	/** Machine-wide coworker (TD-2905). Default on; from setup_state. */
	coworkerEnabled: true,
	/** Screen-pane glow (TD-3402). Default on; from setup_state. */
	cuGlow: true,
	/** Screen-pane agent cursor (TD-3402). Default on; from setup_state. */
	cuAgentCursor: true,
	/** Host overlay on the real display (TD-3402). Default off. */
	cuShowOnRealDisplay: false,
	/** MCP section, from setup_state (TD-4403). */
	mcpServers: [] as McpServerInfo[],
	/** Server name whose save/toggle/remove is in flight; acked by setup_state. */
	savingMcp: null as string | null,
});

let started = false;

/** Register the reducer and apply the stored theme. Unsubscribe for tests. */
export function startSettings(): () => void {
	if (started) return () => {};
	started = true;
	settings.theme = readStoredTheme();
	applyTheme(settings.theme);
	const off = onEvent(reduce);
	return () => {
		started = false;
		off();
	};
}

/** Reset for tests. */
export function resetSettings(): void {
	settings.open = false;
	settings.section = "appearance";
	settings.theme = "system";
	settings.presets = [];
	settings.activePreset = null;
	settings.tierSlugs = {};
	settings.savingTier = null;
	settings.hasApiKey = false;
	settings.keyRequired = true;
	settings.rules = [];
	settings.rulesSessionId = null;
	settings.skipAllApprovals = false;
	settings.loadGlobalMemory = false;
	settings.coworkerEnabled = true;
	settings.cuGlow = true;
	settings.cuAgentCursor = true;
	settings.cuShowOnRealDisplay = false;
	settings.mcpServers = [];
	settings.savingMcp = null;
	started = false;
}

function reduce(event: DaemonEventUnion): void {
	if (event.type === "setup_state") {
		settings.presets = event.presets;
		settings.activePreset = event.active_preset;
		// A daemon too old to send the field leaves the model section empty
		// rather than inventing slugs it was never given.
		settings.tierSlugs = event.tier_slugs ?? {};
		settings.hasApiKey = event.has_api_key;
		settings.keyRequired = event.key_required;
		// setup_state is the ack for set_tier_slug, so it ends the save.
		settings.savingTier = null;
		settings.skipAllApprovals = event.skip_all_approvals ?? false;
		settings.loadGlobalMemory = event.load_global_memory ?? false;
		settings.coworkerEnabled = event.coworker_enabled ?? true;
		settings.cuGlow = event.cu_glow ?? true;
		settings.cuAgentCursor = event.cu_agent_cursor ?? true;
		settings.cuShowOnRealDisplay = event.cu_show_on_real_display ?? false;
		// setup_state is also the ack for the MCP edits (TD-4403), so it ends
		// their save and refreshes the list from the daemon, never a splice.
		settings.mcpServers = event.mcp_servers ?? [];
		settings.savingMcp = null;
		return;
	}
	if (event.type === "policy_rules") {
		settings.rules = event.rules;
	}
}

// ── Opening ───────────────────────────────────────────────────────────

export function openSettings(section: SettingsSection = "appearance"): void {
	settings.open = true;
	settings.section = section;
}

export function closeSettings(): void {
	settings.open = false;
}

export function setSection(section: SettingsSection): void {
	settings.section = section;
}

// ── Appearance ────────────────────────────────────────────────────────

function readStoredTheme(): Theme {
	if (typeof localStorage === "undefined") return "system";
	const stored = localStorage.getItem(THEME_KEY);
	return THEMES.includes(stored as Theme) ? (stored as Theme) : "system";
}

/** Stamp the choice on <html>, or clear it for "system".
 *
 * "system" *removes* the attribute rather than resolving to light or dark in
 * JS. tokens.css already answers that question with a media query, and it
 * re-answers it when the OS flips — resolving here would freeze the choice at
 * whatever the OS happened to be when the app started.
 */
function applyTheme(theme: Theme): void {
	if (typeof document === "undefined") return;
	const root = document.documentElement;
	if (theme === "system") root.removeAttribute("data-theme");
	else root.setAttribute("data-theme", theme);
}

export function setTheme(theme: Theme): void {
	settings.theme = theme;
	applyTheme(theme);
	if (typeof localStorage !== "undefined") localStorage.setItem(THEME_KEY, theme);
}

// ── Model ─────────────────────────────────────────────────────────────

/** True when the tier leaves its model to the endpoint (TD-1805).
 *
 * Rendered as "discovered", never as an empty field: an empty box invites a
 * save, and saving would pin a model the user meant to leave floating.
 */
export function isDiscovered(tier: string): boolean {
	return settings.tierSlugs[tier] === null;
}

/** Persist a tier's slug. No-ops on a blank value or with no active preset. */
export function saveSlug(tier: string, slug: string): void {
	const preset = settings.activePreset;
	const trimmed = slug.trim();
	if (preset === null || trimmed === "") return;
	settings.savingTier = tier;
	const sent = sendToDaemon({ type: "set_tier_slug", preset, tier, slug: trimmed });
	if (!sent) settings.savingTier = null;
}

// ── Policy ────────────────────────────────────────────────────────────

/** Ask for a workspace's saved rules (TD-803).
 *
 * Rules live in the workspace, so they need a session. With none attached the
 * section shows an empty state — an empty list would read as "no rules",
 * which is a different claim than "nothing to read yet".
 */
export function loadRules(sessionId: string | null): void {
	settings.rulesSessionId = sessionId;
	if (sessionId === null) {
		settings.rules = [];
		return;
	}
	sendToDaemon({ type: "list_policy_rules", session_id: sessionId });
}

export function revokeRule(tool: string, args: string): void {
	const sessionId = settings.rulesSessionId;
	if (sessionId === null) return;
	// The daemon answers with a fresh policy_rules, so the list refreshes
	// from the daemon rather than from a local splice.
	sendToDaemon({ type: "revoke_policy_rule", session_id: sessionId, tool, args });
}

/** Turn skip-all on or off (TD-804). The daemon acks with setup_state. */
export function setSkipAllApprovals(enabled: boolean): void {
	sendToDaemon({ type: "set_skip_all_approvals", enabled });
}

/** Turn global memory on or off (TD-2603). Acked with setup_state. */
export function setLoadGlobalMemory(enabled: boolean): void {
	sendToDaemon({ type: "set_load_global_memory", enabled });
}

/** Turn coworker mode on or off (TD-2905). Acked with setup_state. */
export function setCoworker(enabled: boolean): void {
	sendToDaemon({ type: "set_coworker", enabled });
}

/** Persist computer-use indicator prefs (TD-3402). Acked with setup_state. */
export function setCuIndicators(next: {
	glow?: boolean;
	agentCursor?: boolean;
	showOnRealDisplay?: boolean;
}): void {
	sendToDaemon({
		type: "set_cu_indicators",
		glow: next.glow ?? settings.cuGlow,
		agent_cursor: next.agentCursor ?? settings.cuAgentCursor,
		show_on_real_display: next.showOnRealDisplay ?? settings.cuShowOnRealDisplay,
	});
}

// ── MCP servers (TD-4403) ─────────────────────────────────────────────

/** Split a command line into argv, or null if the quoting is broken.
 *
 * The form takes one line; the daemon stores an argv list. Only double
 * quotes group here — nothing is ever run through a shell, so shell
 * quoting rules beyond that do not apply. Null keeps the Add button off.
 */
export function splitCommand(line: string): string[] | null {
	const argv: string[] = [];
	let current = "";
	let open = false;
	for (const ch of line.trim()) {
		if (ch === '"') {
			open = !open;
		} else if (/\s/.test(ch) && !open) {
			if (current !== "") argv.push(current);
			current = "";
		} else {
			current += ch;
		}
	}
	if (open || (argv.length === 0 && current === "")) return null;
	if (current !== "") argv.push(current);
	return argv;
}

/** Add or replace one stdio server. Acked with setup_state. */
export function saveMcpServer(name: string, commandLine: string): boolean {
	const cleaned = name.trim();
	const command = splitCommand(commandLine);
	if (cleaned === "" || command === null) return false;
	settings.savingMcp = cleaned;
	const sent = sendToDaemon({ type: "set_mcp_server", name: cleaned, command });
	if (!sent) settings.savingMcp = null;
	return sent;
}

/** Enable or disable one server. Acked with setup_state. */
export function setMcpEnabled(name: string, enabled: boolean): void {
	settings.savingMcp = name;
	const sent = sendToDaemon({ type: "set_mcp_enabled", name, enabled });
	if (!sent) settings.savingMcp = null;
}

/** Remove one server entirely. Acked with setup_state. */
export function removeMcpServer(name: string): void {
	settings.savingMcp = name;
	const sent = sendToDaemon({ type: "remove_mcp_server", name });
	if (!sent) settings.savingMcp = null;
}
