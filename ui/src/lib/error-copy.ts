// Error → user-facing copy (TD-1008).
//
// Pure mapping, no Svelte: every failure the daemon can surface carries a
// typed cause (turn_complete.error_code, a paused-session reason prefix, or
// an error event's code) and resolves here to tailored copy that names the
// fix. The notification store (notifications.svelte.ts) owns the state; this
// module only decides what a failure says and whether it is a transient
// toast or a blocking banner. Keeping it plain TS keeps the copy reviewable
// and unit-testable without a DOM.
//
// Copy rules:
//   - actionable: every body says what to do, never a raw traceback
//   - banner when the user must act before work continues (missing/invalid
//     key, permission, cap hit, session failed)
//   - toast when the failure is transient or the next action may succeed
//     (rate limit, provider 5xx, context overflow — the loop compacts)

export type NoticeSeverity = "toast" | "banner";

export interface NoticeSpec {
	severity: NoticeSeverity;
	title: string;
	body: string;
}

// ── Checkpoint / memory degradation notices (TD-705, TD-2104) ────────────

/** Title per notice code. The body is the daemon's own message. */
const CHECKPOINT_NOTICE_TITLES: Record<string, string> = {
	no_git: "Checkpoints are off",
	dirty_baseline: "Checkpoints skip your own changes",
	rebase_in_progress: "Checkpoints paused",
	git_error: "Checkpoint failed",
	memory_no_git: "Memory edits are not versioned",
};

/**
 * Copy for a one-time checkpoint or memory degradation notice.
 *
 * The body is the daemon's message verbatim: it already names the
 * workspace condition and what it means for the session, it is the side
 * that knows which of the two rails raised it, and restating it here is
 * two copies of the same sentence drifting apart in two languages.
 *
 * A toast, not a banner: none of these stop work — checkpoints degrade,
 * the turn still runs. The daemon sends each code at most once per
 * session and the store keys them per code, so they neither stack nor
 * repeat. An unrecognised code still says something, because a code the
 * daemon added before this table did is exactly how a notice goes silent.
 */
export function checkpointNoticeCopy(code: string, message: string): NoticeSpec {
	return {
		severity: "toast",
		title: CHECKPOINT_NOTICE_TITLES[code] ?? "Checkpoint notice",
		body: message,
	};
}

// ── Turn failures (turn_complete.failed + error_code) ────────────────────

const TURN_ERROR_COPY: Record<string, NoticeSpec> = {
	missing_api_key: {
		severity: "banner",
		title: "No API key stored",
		body: "Store a key from the title-bar gear → Provider API key, then resend. The conversation is preserved.",
	},
	// TD-1805: a local tier may leave its model tag to the endpoint, so a
	// failure here is "no model server ready", never a bad key. A banner
	// because nothing works until the user acts. The endpoint itself isn't
	// on this event, so the copy points at diagnostics, which prints it.
	model_unresolved: {
		severity: "banner",
		title: "No local model available",
		body: "The local model server named in config.yaml didn't answer with a model to use. Start it (Ollama, vLLM, LM Studio, llama.cpp) and resend — or set slug: on the tier to name one. Run diagnostics from the title-bar gear to see the endpoint and the exact reason. The conversation is preserved.",
	},
	auth_failed: {
		severity: "banner",
		title: "API key rejected",
		body: "The provider rejected the stored key (401). Re-enter a valid key: title-bar gear → Provider API key. Check the base_url in config.yaml matches the key's provider, and resend.",
	},
	// A banner, not a toast: the key is valid and the request well-formed, but
	// nothing will succeed until the account is topped up, so this blocks work
	// exactly the way a missing key does.
	insufficient_credits: {
		severity: "banner",
		title: "Out of provider credits",
		body: "The provider refused the request for lack of credit (402). Top up your account with the provider that issued the key — for OpenRouter, https://openrouter.ai/settings/credits — then resend. The conversation is preserved.",
	},
	forbidden: {
		severity: "banner",
		title: "Access denied",
		body: "The provider refused the request (403). Check that the API key has access to this model and endpoint.",
	},
	rate_limited: {
		severity: "toast",
		title: "Rate limited",
		body: "The provider is throttling requests (429). Wait a moment and resend.",
	},
	context_length_exceeded: {
		severity: "toast",
		title: "Context window exceeded",
		body: "The request was too large. The next message compacts context and may succeed; otherwise switch to a larger-context model in config.yaml or start a fresh session.",
	},
	server_error: { severity: "toast", title: "Provider error", body: "The provider returned an internal error (500). Resend in a moment." },
	bad_gateway: { severity: "toast", title: "Provider unreachable", body: "Bad gateway from the provider (502). Resend in a moment." },
	service_unavailable: {
		severity: "toast",
		title: "Provider unavailable",
		body: "The provider is overloaded (503). Resend in a moment.",
	},
	gateway_timeout: { severity: "toast", title: "Provider timed out", body: "The provider gateway timed out (504). Resend in a moment." },
	bad_request: { severity: "toast", title: "Request rejected", body: "The provider rejected the request (400). If this repeats, report it with copy diagnostics." },
	not_found: { severity: "toast", title: "Model not found", body: "The provider does not serve this model (404). Check the tier model slugs in config.yaml." },
	// Transport/parse failures — live codes emitted by provider.py exception
	// paths (verify against _STATUS_CODE_MAP alone and you'd miss these).
	timeout: {
		severity: "toast",
		title: "Provider timed out",
		body: "The provider didn't answer in time. Resend to retry.",
	},
	connection_error: {
		severity: "toast",
		title: "Can't reach the provider",
		body: "The provider couldn't be reached — check the network, VPN, or firewall, then resend.",
	},
	request_error: {
		severity: "toast",
		title: "Request failed",
		body: "The provider request failed in transit. Resend; if it repeats, copy diagnostics and report it.",
	},
	parse_error: {
		severity: "toast",
		title: "Unreadable provider response",
		body: "The provider's response couldn't be parsed. If this repeats, copy diagnostics and report it — it usually means a provider-side change.",
	},
	// Distinct from stream_interrupted: nothing arrived at all, so there is no
	// partial answer to explain away. Naming the provider matters — the turn
	// used to surface as "the model finished without a reply", which blames
	// the model for output the provider never sent.
	already_started: {
		severity: "toast",
		title: "Grok is already running",
		body: "This session already has a Grok agent. Send another message, or start a new session.",
	},
	agent_exited: {
		severity: "toast",
		title: "Grok stopped",
		body: "The Grok CLI closed. Send another message to start it again.",
	},
	empty_stream: {
		severity: "toast",
		title: "Provider sent nothing",
		body: "The provider accepted the request, then closed the stream without sending any output. Nothing was generated and nothing was charged. Resend to retry.",
	},
	stream_interrupted: {
		severity: "toast",
		title: "Response interrupted",
		body: "The provider's stream dropped mid-turn. Resend to retry the turn.",
	},
};

/** Copy for a failed turn's typed error code, or null to stay silent (cancelled). */
export function turnFailureCopy(errorCode: string | null): NoticeSpec | null {
	if (errorCode === null || errorCode === "cancelled") return null;
	const known = TURN_ERROR_COPY[errorCode];
	if (known) return known;
	// Unknown codes still never surface as tracebacks, just as a named code.
	return {
		severity: "toast",
		title: "Turn failed",
		body: `The turn ended with an unrecognised error (${errorCode}). Copy diagnostics and report it.`,
	};
}

// ── Session states ───────────────────────────────────────────────────────

/** Copy for a session_state event, or null when the state needs no notice. */
export function sessionStateCopy(state: string, reason: string | null): NoticeSpec | null {
	if (state === "paused") {
		const why = reason ?? "paused";
		if (why.startsWith("spend cap exceeded")) {
			return {
				severity: "banner",
				title: "Spend cap reached",
				body: `${why}. Review spend, then resume — or raise spend_usd under caps: in .tst/config.yaml.`,
			};
		}
		if (why.startsWith("wall-clock cap exceeded")) {
			return {
				severity: "banner",
				title: "Wall-clock cap reached",
				body: `${why}. Resume to continue, or raise wall_clock_hours under caps: in .tst/config.yaml.`,
			};
		}
		if (why.startsWith("iteration cap exceeded")) {
			return {
				severity: "banner",
				title: "Iteration cap reached",
				body: `${why}. Resume to continue, or raise max_iterations under caps: in .tst/config.yaml.`,
			};
		}
		return { severity: "banner", title: "Session paused", body: why };
	}
	if (state === "interrupted") {
		// Daemon stopped mid-session; the loop is not resumable — the tombstone
		// banner must say so rather than go quiet.
		return {
			severity: "banner",
			title: "Session can’t be continued",
			body: "This session has no saved model conversation, so it cannot continue honestly. If messages appear they are whatever was last written to disk. Start a new session to keep working.",
		};
	}
	if (state === "failed") {
		return {
			severity: "banner",
			title: "Session failed",
			body: `${reason ?? "The session ended unexpectedly."} Copy diagnostics and report it if this repeats.`,
		};
	}
	return null;
}

// ── Daemon error events ──────────────────────────────────────────────────

/** Copy for a daemon `error` event: code + daemon-authored message (already human). */
export function daemonErrorCopy(code: string, message: string): NoticeSpec {
	if (code === "keychain_locked") {
		// TD-1105: locked or password-drifted login keychain. The daemon's
		// message already carries the unlock steps; the wizard keeps the
		// typed key, so Store again is the retry once it's unlocked.
		return {
			severity: "banner",
			title: "Keychain is locked",
			body: `${message} The key you typed is still in the wizard — click Store key again once it's unlocked.`,
		};
	}
	if (code === "session_not_running") {
		// TD-1711: the send targeted a dead session (tombstone or terminal).
		// The daemon's message already names the fix (start a new session,
		// resend) — surface it verbatim rather than paraphrasing over it.
		return {
			severity: "toast",
			title: "That session has ended",
			body: message,
		};
	}
	if (code === "session_busy") {
		// TD-1715: Delete or Move aimed at a session mid-turn. The daemon's
		// message already names the way out (wait, stop, or archive instead),
		// and the rail shows it under the row that asked — so this stays a
		// toast, not a banner: nothing is broken and no other work is blocked.
		return {
			severity: "toast",
			title: "That session is mid-turn",
			body: message,
		};
	}
	if (code.startsWith("attachment_")) {
		// TD-1709: the daemon refused an attachment on arrival — the case a
		// composer-side check cannot cover. Its message already names the file,
		// the cap, and the config key, and ends by saying nothing was sent, so
		// it is surfaced verbatim rather than paraphrased over. A toast, not a
		// banner: the draft is still in the composer and nothing else is blocked.
		return { severity: "toast", title: "Attachment refused", body: message };
	}
	if (code === "workspace_not_found") {
		// TD-1103: the user tried to open a path that doesn't exist (usually
		// from the recents menu — the folder was moved or deleted). The fix
		// is picking the folder again, so the copy points at the picker.
		return {
			severity: "toast",
			title: "Workspace not found",
			body: `${message} If it moved, open the new location with the folder picker and remove the stale entry from the workspace menu.`,
		};
	}
	return { severity: "toast", title: `Daemon error: ${code}`, body: message };
}
