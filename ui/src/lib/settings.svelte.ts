// Settings store (TD-1703).
//
// The title-bar gear opens this, not the wizard — the wizard stays for first
// run only. Everything shown comes from the daemon's `setup_state` and
// `policy_rules`; nothing is inferred locally (AGENTS §6).
//
// Named keys (TD-1717) live on this store as {id, name, stored} only.
// The wizard's first key still uses the onboarding store.
//
// Wiring mirrors doctor.svelte.ts: listens on the connection fan-out, sends
// only via sendToDaemon — no client reference, no import cycle.

import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import type { DaemonEventUnion, PolicyRuleSummary } from "./protocol";

export type SettingsSection =
	| "appearance"
	| "computer"
	| "engine"
	| "model"
	| "policy"
	| "mcp"
	| "key"
	| "about";
export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
	"appearance",
	"computer",
	"engine",
	"model",
	"policy",
	"mcp",
	"key",
	"about",
] as const;

export type CuMode = "background" | "full_control";

export type McpServerRow = {
	id: string;
	transport: "stdio" | "http";
	command: string[];
	url: string;
	enabled: boolean;
};

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
	/** Named keys (TD-1717). Presence only — never the secret. */
	credentials: [] as { id: string; name: string; stored: boolean; base_url?: string | null }[],
	/** Configured binding per tier; null = unbound. */
	tierCredentials: {} as Record<string, string | null>,
	/** Whether each active-preset tier is loopback (offers "no key"). */
	tierLoopback: {} as Record<string, boolean>,
	/** Tier whose credential save is in flight. */
	savingCredentialTier: null as string | null,
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
	/** Host overlay on the real display (TD-3402 addendum). Default on. */
	cuShowOnRealDisplay: true,
	/** Tailscale remote attach (TD-3603). Default off; from setup_state. */
	remoteAttachEnabled: false,
	/** Bound Tailscale address, never a token. */
	remoteBind: null as string | null,
	/** Agent engine for new sessions. */
	engine: "native" as "native" | "grok",
	grokAvailable: false,
	grokBinary: null as string | null,
	savingEngine: false,
	/** Hold-to-talk (TD-4701). Default off; from setup_state. */
	voiceEnabled: false,
	/** Whether config.yaml names a transcription URL. */
	voiceHasEndpoint: false,
	/** TD-4830: computer-use policy. */
	cuEnabled: true,
	cuMode: "background" as CuMode,
	cuUnhideOnFinish: true,
	cuDeniedApps: [] as string[],
	/** Listed MCP servers (TD-4403). From setup_state, never inferred. */
	mcpServers: [] as McpServerRow[],
	/** Hold-to-talk (TD-4701). From setup_state; the URL never arrives. */
	speechEnabled: false,
	speechReady: false,
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
	settings.credentials = [];
	settings.tierCredentials = {};
	settings.tierLoopback = {};
	settings.savingCredentialTier = null;
	settings.rules = [];
	settings.rulesSessionId = null;
	settings.skipAllApprovals = false;
	settings.loadGlobalMemory = false;
	settings.coworkerEnabled = true;
	settings.cuGlow = true;
	settings.cuAgentCursor = true;
	settings.cuShowOnRealDisplay = true;
	settings.remoteAttachEnabled = false;
	settings.remoteBind = null;
	settings.engine = "native";
	settings.grokAvailable = false;
	settings.grokBinary = null;
	settings.savingEngine = false;
	settings.voiceEnabled = false;
	settings.voiceHasEndpoint = false;
	settings.cuEnabled = true;
	settings.cuMode = "background";
	settings.cuUnhideOnFinish = true;
	settings.cuDeniedApps = [];
	settings.mcpServers = [];
	settings.speechEnabled = false;
	settings.speechReady = false;
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
		settings.credentials = event.credentials ?? [];
		settings.tierCredentials = event.tier_credentials ?? {};
		settings.tierLoopback = event.tier_loopback ?? {};
		// setup_state is the ack for set_tier_slug / set_tier_credential.
		settings.savingTier = null;
		settings.savingCredentialTier = null;
		settings.skipAllApprovals = event.skip_all_approvals ?? false;
		settings.loadGlobalMemory = event.load_global_memory ?? false;
		settings.coworkerEnabled = event.coworker_enabled ?? true;
		settings.cuGlow = event.cu_glow ?? true;
		settings.cuAgentCursor = event.cu_agent_cursor ?? true;
		settings.cuShowOnRealDisplay = event.cu_show_on_real_display ?? true;
		settings.remoteAttachEnabled = event.remote_attach_enabled ?? false;
		settings.remoteBind = event.remote_bind ?? null;
		settings.engine = event.engine ?? "native";
		settings.grokAvailable = event.grok_available ?? false;
		settings.grokBinary = event.grok_binary ?? null;
		settings.savingEngine = false;
		settings.voiceEnabled = event.voice_enabled ?? false;
		settings.voiceHasEndpoint = event.voice_has_endpoint ?? false;
		settings.cuEnabled = event.cu_enabled ?? true;
		settings.cuMode = event.cu_mode === "full_control" ? "full_control" : "background";
		settings.cuUnhideOnFinish = event.cu_unhide_on_finish ?? true;
		settings.cuDeniedApps = event.cu_denied_apps ?? [];
		settings.mcpServers = (event.mcp_servers ?? []).map((row) => ({
			id: row.id,
			transport: row.transport,
			command: row.command ?? [],
			url: row.url ?? "",
			enabled: row.enabled ?? true,
		}));
		settings.speechEnabled = event.speech_enabled ?? false;
		settings.speechReady = event.speech_ready ?? false;
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

export function setEngine(kind: "native" | "grok"): void {
	settings.savingEngine = true;
	const sent = sendToDaemon({ type: "set_engine", kind });
	if (!sent) settings.savingEngine = false;
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

/** Host the selected named key talks to, or null to keep the tier URL. */
export function credentialHost(credentialId: string): string | null {
	const row = settings.credentials.find((c) => c.id === credentialId);
	return row?.base_url ?? null;
}

/** Persist a tier's named key. Empty string unbinds. */
export function saveTierCredential(tier: string, credential: string): void {
	const preset = settings.activePreset;
	if (preset === null) return;
	settings.savingCredentialTier = tier;
	const sent = sendToDaemon({
		type: "set_tier_credential",
		preset,
		tier,
		credential,
	});
	if (!sent) settings.savingCredentialTier = null;
}

/** Selected catalog id for the picker: bound, else implicit openrouter on remote. */
export function selectedCredential(tier: string): string {
	const bound = settings.tierCredentials[tier];
	if (bound) return bound;
	if (settings.tierLoopback[tier]) return "";
	return "openrouter";
}

export function storeNamedKey(name: string, apiKey: string, credential?: string): void {
	const trimmedName = name.trim();
	const trimmedKey = apiKey.trim();
	if (trimmedName === "" || trimmedKey === "") return;
	sendToDaemon({
		type: "set_api_key",
		api_key: trimmedKey,
		name: trimmedName,
		credential: credential ?? null,
	});
}

export function renameCredential(credential: string, name: string): void {
	const trimmed = name.trim();
	if (trimmed === "") return;
	sendToDaemon({ type: "set_credential", credential, name: trimmed });
}

export function deleteNamedKey(credential: string): void {
	if (credential.trim() === "") return;
	sendToDaemon({ type: "delete_credential", credential });
}

export function validateNamedKey(credential: string, apiKey?: string): void {
	const typed = apiKey?.trim();
	sendToDaemon(
		typed
			? { type: "validate_api_key", credential, api_key: typed }
			: { type: "validate_api_key", credential },
	);
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

/** Turn hold-to-talk dictation on or off (TD-4701). Acked with setup_state. */
export function setVoice(enabled: boolean): void {
	sendToDaemon({ type: "set_voice", enabled });
}

/** Turn remote attach on or off (TD-3603). Acked with setup_state. */
export function setRemoteAttach(enabled: boolean): void {
	sendToDaemon({ type: "set_remote_attach", enabled });
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

/** Persist computer-use policy (TD-4830). Acked with setup_state. */
export function setCuPolicy(next: {
	enabled?: boolean;
	mode?: CuMode;
	unhideOnFinish?: boolean;
	deniedApps?: string[];
}): void {
	sendToDaemon({
		type: "set_cu_policy",
		enabled: next.enabled ?? settings.cuEnabled,
		mode: next.mode ?? settings.cuMode,
		unhide_on_finish: next.unhideOnFinish ?? settings.cuUnhideOnFinish,
		denied_apps: next.deniedApps ?? settings.cuDeniedApps,
	});
}

// ── MCP servers (TD-4403) ────────────────────────────────────────────

/** Persist one listed server. Command is argv tokens; there is no env. */
export function saveMcpServer(server: McpServerRow): void {
	const id = server.id.trim();
	if (id === "") return;
	sendToDaemon({
		type: "set_mcp_server",
		id,
		transport: server.transport,
		command: server.command,
		url: server.url,
		enabled: server.enabled,
	});
}

/** Disable or re-enable a server the daemon already listed. */
export function setMcpServerEnabled(id: string, enabled: boolean): void {
	const row = settings.mcpServers.find((s) => s.id === id);
	if (!row) return;
	saveMcpServer({ ...row, enabled });
}

/** Remove a listed server. */
export function deleteMcpServer(id: string): void {
	if (id.trim() === "") return;
	sendToDaemon({ type: "delete_mcp_server", id });
}
