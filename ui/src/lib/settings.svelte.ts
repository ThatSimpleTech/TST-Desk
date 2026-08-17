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
import type { DaemonEventUnion, PolicyRuleSummary } from "./protocol";

export type SettingsSection = "appearance" | "model" | "policy" | "key";
export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
	"appearance",
	"model",
	"policy",
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
