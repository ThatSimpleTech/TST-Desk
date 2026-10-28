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

import { isBridged } from "./bridged-id";
import { onEvent, sendToDaemon } from "./connection-status.svelte.js";
import { reducePairing } from "./pair-device.svelte.js";
import type {
	DaemonEventUnion,
	GithubPhase,
	GooglePhase,
	MicrosoftPhase,
	PolicyRuleSummary,
	RemoteDeviceSummary,
	TierCard,
} from "./protocol";

export type SettingsSection =
	| "appearance"
	| "devices"
	| "machines"
	| "computer"
	| "engine"
	| "model"
	| "budget"
	| "policy"
	| "mcp"
	| "key"
	| "email"
	| "github"
	| "about";
export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
	"appearance",
	"devices",
	"machines",
	"computer",
	"engine",
	"model",
	"budget",
	"policy",
	"mcp",
	"key",
	"email",
	"github",
	"about",
] as const;

export type CuMode = "background" | "full_control";

export type McpServerRow = {
	id: string;
	transport: "stdio" | "http";
	command: string[];
	url: string;
	enabled: boolean;
	/** Tools left out at the last load (TD-5104). */
	excludedTools?: { name: string; reason: string }[];
	/** Env vars (stdio) or headers (http) by name (TD-5110). Never a value. */
	values?: McpValueRow[];
};

export type McpValueRow = {
	name: string;
	source: "literal" | "keychain";
	stored: boolean;
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
	/** Picker display names per preset (config `label`). */
	presetLabels: {} as Record<string, string>,
	/** Presets the picker lists under "More models" (config `picker: more`). */
	morePresets: [] as string[],
	/** One-line picker subtitle per preset (config `description`, TD-5172). */
	presetDescriptions: {} as Record<string, string>,
	/** Presets the picker lists under "Teams" (config `picker: team`). */
	teamPresets: [] as string[],
	/** False until a model is set up: the first run starts "Add a model". */
	hasModel: true,
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
	/** TD-5185: stored prices and limits per tier, and the last card refresh. */
	tierCards: {} as Record<string, TierCard>,
	/** A model-card refresh is in flight; cleared by setup_state or an error. */
	refreshingCards: false,
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
	remotePort: null as number | null,
	/** Paired phones (TD-5014). From setup_state; never a token or hash. */
	remoteDevices: [] as RemoteDeviceSummary[],
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
	/** TD-708 dev: judgment-seam toggles. All default off; from setup_state. */
	judgmentsVerification: false,
	judgmentsSemanticBreaker: false,
	judgmentsCandidateSelection: false,
	judgmentsConfidenceThreshold: 0.6,
	judgmentsMaxStateChars: 2000,
	/** The connector, the TypeSafe key's credential id, and key presence. */
	judgmentsBackend: "worker" as "worker" | "typesafe",
	judgmentsTypesafeCredential: "typesafe",
	judgmentsTypesafeKeyStored: false,
	/** Listed MCP servers (TD-4403). From setup_state, never inferred. */
	mcpServers: [] as McpServerRow[],
	/** Hold-to-talk (TD-4701). From setup_state; the URL never arrives. */
	speechEnabled: false,
	speechReady: false,
	/** Email delivery (TD-3820, TD-4853, TD-4854). From setup_state. No password, no token. */
	emailEnabled: false,
	emailMethod: "smtp" as "smtp" | "microsoft" | "google",
	emailHost: "",
	emailPort: 587,
	emailSecurity: "starttls" as "starttls" | "tls",
	emailUsername: "",
	emailFrom: "",
	emailPasswordStored: false,
	/** Last test_email result. Not cleared by setup_state. */
	emailTestOk: null as boolean | null,
	emailTestErrorClass: "",
	emailTestMessage: "",
	/** GitHub connector (TD-4852). From setup_state and github_status. */
	githubEnabled: false,
	githubConnected: false,
	githubPhase: "off" as GithubPhase,
	githubUserCode: "",
	githubVerificationUri: "",
	githubLogin: null as string | null,
	githubMessage: "",
	/** Microsoft sign-in (TD-4853). From setup_state and microsoft_status. */
	microsoftEnabled: false,
	microsoftConnected: false,
	microsoftPhase: "off" as MicrosoftPhase,
	microsoftAccount: null as string | null,
	microsoftMessage: "",
	/** Google sign-in (TD-4854). From setup_state and google_status. */
	googleEnabled: false,
	googleConnected: false,
	googlePhase: "off" as GooglePhase,
	googleAccount: null as string | null,
	googleMessage: "",
	googleClientSecretStored: false,
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
	settings.presetLabels = {};
	settings.morePresets = [];
	settings.presetDescriptions = {};
	settings.teamPresets = [];
	settings.hasModel = true;
	settings.activePreset = null;
	settings.tierSlugs = {};
	settings.savingTier = null;
	settings.hasApiKey = false;
	settings.keyRequired = true;
	settings.credentials = [];
	settings.tierCredentials = {};
	settings.tierLoopback = {};
	settings.tierCards = {};
	settings.refreshingCards = false;
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
	settings.remotePort = null;
	settings.remoteDevices = [];
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
	settings.judgmentsVerification = false;
	settings.judgmentsSemanticBreaker = false;
	settings.judgmentsCandidateSelection = false;
	settings.judgmentsConfidenceThreshold = 0.6;
	settings.judgmentsMaxStateChars = 2000;
	settings.judgmentsBackend = "worker";
	settings.judgmentsTypesafeCredential = "typesafe";
	settings.judgmentsTypesafeKeyStored = false;
	settings.mcpServers = [];
	settings.speechEnabled = false;
	settings.speechReady = false;
	settings.emailEnabled = false;
	settings.emailMethod = "smtp";
	settings.emailHost = "";
	settings.emailPort = 587;
	settings.emailSecurity = "starttls";
	settings.emailUsername = "";
	settings.emailFrom = "";
	settings.emailPasswordStored = false;
	settings.emailTestOk = null;
	settings.emailTestErrorClass = "";
	settings.emailTestMessage = "";
	settings.githubEnabled = false;
	settings.githubConnected = false;
	settings.githubPhase = "off";
	settings.githubUserCode = "";
	settings.githubVerificationUri = "";
	settings.githubLogin = null;
	settings.githubMessage = "";
	settings.microsoftEnabled = false;
	settings.microsoftConnected = false;
	settings.microsoftPhase = "off";
	settings.microsoftAccount = null;
	settings.microsoftMessage = "";
	settings.googleEnabled = false;
	settings.googleConnected = false;
	settings.googlePhase = "off";
	settings.googleAccount = null;
	settings.googleMessage = "";
	settings.googleClientSecretStored = false;
	started = false;
}

const GITHUB_PHASES: readonly GithubPhase[] = [
	"off",
	"idle",
	"pending",
	"connected",
	"expired",
	"denied",
	"error",
];

function applyGithub(github: {
	enabled?: boolean;
	connected?: boolean;
	phase?: string;
	user_code?: string;
	verification_uri?: string;
	login?: string | null;
	message?: string;
} | undefined): void {
	settings.githubEnabled = github?.enabled ?? false;
	settings.githubConnected = github?.connected ?? false;
	const phase = github?.phase;
	settings.githubPhase = GITHUB_PHASES.includes(phase as GithubPhase)
		? (phase as GithubPhase)
		: "off";
	settings.githubUserCode = github?.user_code ?? "";
	settings.githubVerificationUri = github?.verification_uri ?? "";
	settings.githubLogin = github?.login ?? null;
	settings.githubMessage = github?.message ?? "";
}

const MICROSOFT_PHASES: readonly MicrosoftPhase[] = [
	"off",
	"idle",
	"pending",
	"connected",
	"denied",
	"error",
	"expired",
];

function applyMicrosoft(microsoft: {
	enabled?: boolean;
	connected?: boolean;
	phase?: string;
	account?: string | null;
	message?: string;
} | undefined): void {
	settings.microsoftEnabled = microsoft?.enabled ?? false;
	settings.microsoftConnected = microsoft?.connected ?? false;
	const phase = microsoft?.phase;
	settings.microsoftPhase = MICROSOFT_PHASES.includes(phase as MicrosoftPhase)
		? (phase as MicrosoftPhase)
		: "off";
	settings.microsoftAccount = microsoft?.account ?? null;
	settings.microsoftMessage = microsoft?.message ?? "";
}

const GOOGLE_PHASES: readonly GooglePhase[] = [
	"off",
	"idle",
	"pending",
	"connected",
	"denied",
	"error",
	"expired",
];

function applyGoogle(google: {
	enabled?: boolean;
	connected?: boolean;
	phase?: string;
	account?: string | null;
	message?: string;
	client_secret_stored?: boolean;
} | undefined): void {
	settings.googleEnabled = google?.enabled ?? false;
	settings.googleConnected = google?.connected ?? false;
	const phase = google?.phase;
	settings.googlePhase = GOOGLE_PHASES.includes(phase as GooglePhase)
		? (phase as GooglePhase)
		: "off";
	settings.googleAccount = google?.account ?? null;
	settings.googleMessage = google?.message ?? "";
	settings.googleClientSecretStored = google?.client_secret_stored ?? false;
}

function reduce(event: DaemonEventUnion): void {
	reducePairing(event);
	if (event.type === "error") settings.refreshingCards = false;
	if (event.type === "setup_state") {
		settings.presets = event.presets;
		settings.presetLabels = event.preset_labels ?? {};
		settings.morePresets = event.more_presets ?? [];
		settings.presetDescriptions = event.preset_descriptions ?? {};
		settings.teamPresets = event.team_presets ?? [];
		settings.hasModel = event.has_model ?? true;
		settings.activePreset = event.active_preset;
		// A daemon too old to send the field leaves the model section empty
		// rather than inventing slugs it was never given.
		settings.tierSlugs = event.tier_slugs ?? {};
		settings.hasApiKey = event.has_api_key;
		settings.keyRequired = event.key_required;
		settings.credentials = event.credentials ?? [];
		settings.tierCredentials = event.tier_credentials ?? {};
		settings.tierLoopback = event.tier_loopback ?? {};
		settings.tierCards = event.tier_cards ?? {};
		// setup_state is the ack for set_tier_slug / set_tier_credential /
		// refresh_model_cards.
		settings.refreshingCards = false;
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
		settings.remotePort = event.remote_port ?? null;
		settings.remoteDevices = event.remote_devices ?? [];
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
		settings.judgmentsVerification = event.judgments_verification ?? false;
		settings.judgmentsSemanticBreaker = event.judgments_semantic_breaker ?? false;
		settings.judgmentsCandidateSelection = event.judgments_candidate_selection ?? false;
		settings.judgmentsConfidenceThreshold = event.judgments_confidence_threshold ?? 0.6;
		settings.judgmentsMaxStateChars = event.judgments_max_state_chars ?? 2000;
		settings.judgmentsBackend = event.judgments_backend === "typesafe" ? "typesafe" : "worker";
		settings.judgmentsTypesafeCredential =
			event.judgments_typesafe_credential ?? "typesafe";
		settings.judgmentsTypesafeKeyStored = event.judgments_typesafe_key_stored ?? false;
		settings.mcpServers = (event.mcp_servers ?? []).map((row) => ({
			id: row.id,
			transport: row.transport,
			command: row.command ?? [],
			url: row.url ?? "",
			enabled: row.enabled ?? true,
			excludedTools: row.excluded_tools ?? [],
			values: [...(row.env ?? []), ...(row.headers ?? [])].map((v) => ({
				name: v.name,
				source: v.source,
				stored: v.stored,
			})),
		}));
		settings.speechEnabled = event.speech_enabled ?? false;
		settings.speechReady = event.speech_ready ?? false;
		const email = event.email;
		settings.emailEnabled = email?.enabled ?? false;
		settings.emailMethod =
			email?.method === "microsoft" || email?.method === "google" ? email.method : "smtp";
		settings.emailHost = email?.host ?? "";
		settings.emailPort = email?.port ?? 587;
		settings.emailSecurity = email?.security === "tls" ? "tls" : "starttls";
		settings.emailUsername = email?.username ?? "";
		settings.emailFrom = email?.from_address ?? "";
		settings.emailPasswordStored = email?.password_stored ?? false;
		applyGithub(event.github);
		applyMicrosoft(event.microsoft);
		applyGoogle(event.google);
		return;
	}
	if (event.type === "github_status") {
		applyGithub(event);
		return;
	}
	if (event.type === "microsoft_status") {
		applyMicrosoft(event);
		return;
	}
	if (event.type === "google_status") {
		applyGoogle(event);
		return;
	}
	if (event.type === "email_test_result") {
		settings.emailTestOk = event.ok ?? false;
		settings.emailTestErrorClass = event.error_class ?? "";
		settings.emailTestMessage = event.message ?? "";
		return;
	}
	// A late reply for the local session the pane just left is not the
	// remote session's (TD-5015).
	if (event.type === "policy_rules" && !isBridged(settings.rulesSessionId)) {
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

/** TD-5185: re-read the active preset's prices and limits from each host. */
export function refreshModelCards(): void {
	const preset = settings.activePreset;
	if (preset === null || settings.refreshingCards) return;
	settings.refreshingCards = true;
	const sent = sendToDaemon({ type: "refresh_model_cards", preset });
	if (!sent) settings.refreshingCards = false;
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

export function storeNamedKey(
	name: string,
	apiKey: string,
	credential?: string,
	baseUrl?: string,
): void {
	const trimmedName = name.trim();
	const trimmedKey = apiKey.trim();
	if (trimmedName === "" || trimmedKey === "") return;
	const host = (baseUrl ?? "").trim();
	sendToDaemon({
		type: "set_api_key",
		api_key: trimmedKey,
		name: trimmedName,
		credential: credential ?? null,
		...(host !== "" ? { base_url: host } : {}),
	});
}

export function renameCredential(credential: string, name: string): void {
	const trimmed = name.trim();
	if (trimmed === "") return;
	sendToDaemon({ type: "set_credential", credential, name: trimmed });
}

/** Persist the host a named key talks to (TD-1722). Empty clears it. */
export function saveCredentialHost(credential: string, name: string, baseUrl: string): void {
	const trimmedName = name.trim();
	if (credential.trim() === "" || trimmedName === "") return;
	sendToDaemon({
		type: "set_credential",
		credential,
		name: trimmedName,
		base_url: baseUrl.trim(),
	});
}

/** Add a catalog row without a secret (TD-1723). Host is optional. */
export function createNamedSource(name: string, baseUrl?: string): void {
	const trimmedName = name.trim();
	if (trimmedName === "") return;
	const host = (baseUrl ?? "").trim();
	sendToDaemon({
		type: "set_credential",
		name: trimmedName,
		...(host !== "" ? { base_url: host } : {}),
	});
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
	// Another computer's rules live in its workspace, and the hub forwards no
	// policy verb (TD-5015): the section says they stay there instead.
	if (sessionId === null || isBridged(sessionId)) {
		settings.rules = [];
		return;
	}
	sendToDaemon({ type: "list_policy_rules", session_id: sessionId });
}

export function revokeRule(tool: string, args: string): void {
	const sessionId = settings.rulesSessionId;
	if (sessionId === null || isBridged(sessionId)) return;
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

/** New phone token (TD-5005). Acked with setup_state; the token stays in the file. */
export function rotateRemoteToken(): void {
	sendToDaemon({ type: "rotate_remote_token" });
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

/** Persist judgment-seam toggles (TD-708 dev). Acked with setup_state. */
export function setJudgments(next: {
	verification?: boolean;
	semanticBreaker?: boolean;
	candidateSelection?: boolean;
	confidenceThreshold?: number;
	maxStateChars?: number;
	backend?: "worker" | "typesafe";
	typesafeCredential?: string;
}): void {
	sendToDaemon({
		type: "set_judgments",
		verification: next.verification ?? settings.judgmentsVerification,
		semantic_breaker: next.semanticBreaker ?? settings.judgmentsSemanticBreaker,
		candidate_selection: next.candidateSelection ?? settings.judgmentsCandidateSelection,
		confidence_threshold: next.confidenceThreshold ?? settings.judgmentsConfidenceThreshold,
		max_state_chars: next.maxStateChars ?? settings.judgmentsMaxStateChars,
		backend: next.backend ?? settings.judgmentsBackend,
		typesafe_credential: next.typesafeCredential ?? null,
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

/** An env var for a stdio server, a header for an http one (TD-5110). */
function mcpTarget(serverId: string): "env" | "header" | null {
	const row = settings.mcpServers.find((s) => s.id === serverId);
	if (!row) return null;
	return row.transport === "stdio" ? "env" : "header";
}

/** Put one value in the keychain for a listed server (TD-5110). The value
 *  goes straight to the daemon and is never kept in this store. */
export function saveMcpSecret(serverId: string, name: string, value: string): void {
	const target = mcpTarget(serverId);
	const trimmedName = name.trim();
	const trimmedValue = value.trim();
	if (target === null || trimmedName === "" || trimmedValue === "") return;
	sendToDaemon({
		type: "set_mcp_secret",
		server_id: serverId,
		target,
		name: trimmedName,
		value: trimmedValue,
	});
}

/** Drop one env var or header, and its keychain item (TD-5110). */
export function deleteMcpSecret(serverId: string, name: string): void {
	const target = mcpTarget(serverId);
	if (target === null || name.trim() === "") return;
	sendToDaemon({ type: "delete_mcp_secret", server_id: serverId, target, name });
}

// ── Email (TD-3820) ──────────────────────────────────────────────────

/** Persist email settings. An empty password leaves the keychain item alone. */
export function saveEmailNotify(input: {
	enabled: boolean;
	method: "smtp" | "microsoft" | "google";
	host: string;
	port: number;
	security: "starttls" | "tls";
	username: string;
	from_address: string;
	password: string | null;
	googleClientSecret?: string | null;
}): void {
	// Omit an empty secret so a Microsoft or SMTP save stays the same object
	// the form has always sent. A typed secret is sent only for Google.
	const secret = input.googleClientSecret;
	sendToDaemon({
		type: "set_email_notify",
		enabled: input.enabled,
		method: input.method,
		host: input.host,
		port: input.port,
		security: input.security,
		username: input.username,
		from_address: input.from_address,
		password: input.password,
		...(input.method === "google" && secret ? { google_client_secret: secret } : {}),
	});
}

/** Send one short test using the saved config and the keychain password. */
export function sendTestEmail(to: string): void {
	sendToDaemon({ type: "test_email", to });
}

/** Start GitHub device flow. The daemon answers with github_status. */
export function connectGithub(): void {
	sendToDaemon({ type: "github_connect" });
}

/** Delete the GitHub keychain item. */
export function disconnectGithub(): void {
	sendToDaemon({ type: "github_disconnect" });
}

/** Start Microsoft sign-in. The daemon opens the browser and answers with microsoft_status. */
export function connectMicrosoft(): void {
	sendToDaemon({ type: "microsoft_connect" });
}

/** Delete the Microsoft keychain item. */
export function disconnectMicrosoft(): void {
	sendToDaemon({ type: "microsoft_disconnect" });
}

/** Start Google sign-in. The daemon opens the browser and answers with google_status. */
export function connectGoogle(): void {
	sendToDaemon({ type: "google_connect" });
}

/** Delete the Google token item. The desktop client secret stays in the keychain. */
export function disconnectGoogle(): void {
	sendToDaemon({ type: "google_disconnect" });
}
