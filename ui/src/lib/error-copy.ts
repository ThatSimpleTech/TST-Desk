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

// ── Turn failures (turn_complete.failed + error_code) ────────────────────

const TURN_ERROR_COPY: Record<string, NoticeSpec> = {
	missing_api_key: {
		severity: "banner",
		title: "No API key stored",
		body: "Store a key for this provider with `tstd keychain set <provider>`, then resend. The conversation is preserved.",
	},
	auth_failed: {
		severity: "banner",
		title: "API key rejected",
		body: "The provider rejected the stored key (401). Re-store a valid key with `tstd keychain set <provider>`, check the base_url in config.yaml matches the key's provider, and resend.",
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
			title: "Session can’t be resumed",
			body: "The daemon stopped while this session was running. The session is a tombstone — start a new session to keep working.",
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
