// Hooks inspector logic (TD-5134): applies `hooks_state` replies and live
// `hook_run` events, asks the daemon for the attached session's hooks.
// Pure — the .svelte.ts wrapper injects the reactive state and connection.

import type { ClientMessageUnion, DaemonEventUnion, HookInfo, HookRun } from "./protocol";

/** What the Hooks panel renders from. */
export interface HooksViewState {
	/** Session these hooks describe; null until the first reply lands. */
	sessionId: string | null;
	hooks: HookInfo[];
	workspaceState: string;
	workspaceError: string | null;
	loaded: boolean;
}

export function createHooksState(): HooksViewState {
	return { sessionId: null, hooks: [], workspaceState: "none", workspaceError: null, loaded: false };
}

export function clearHooks(state: HooksViewState): void {
	state.sessionId = null;
	state.hooks = [];
	state.workspaceState = "none";
	state.workspaceError = null;
	state.loaded = false;
}

export interface HooksStoreDeps {
	/** Send a client message; false when the socket is down. */
	send: (msg: ClientMessageUnion) => boolean;
}

export function createHooksStore(deps: HooksStoreDeps, state: HooksViewState) {
	return {
		/** Ingest a daemon event; true when the view changed. */
		applyEvent(event: DaemonEventUnion, currentSessionId: string | null): boolean {
			if (event.type === "hooks_state") {
				if (event.session_id !== currentSessionId) return false;
				state.sessionId = event.session_id;
				state.hooks = event.hooks;
				state.workspaceState = event.workspace_state ?? "none";
				state.workspaceError = event.workspace_error ?? null;
				state.loaded = true;
				return true;
			}
			if (event.type === "hook_run") {
				if (event.session_id !== currentSessionId || state.sessionId !== currentSessionId) {
					return false;
				}
				return applyRun(state, event);
			}
			return false;
		},

		/** Ask for the attached session's hooks. A view from another session
		 *  is dropped first: an empty panel is honest, a stale one lies. */
		refresh(currentSessionId: string | null): boolean {
			if (currentSessionId === null) {
				clearHooks(state);
				return false;
			}
			if (state.sessionId !== currentSessionId) clearHooks(state);
			return deps.send({ type: "get_hooks", session_id: currentSessionId });
		},
	};
}

/** A live run becomes its hook's last result. A run for a hook the panel
 *  does not list (config changed since) waits for the next refresh. */
function applyRun(state: HooksViewState, run: HookRun): boolean {
	const index = state.hooks.findIndex((hook) => hook.id === run.hook_id);
	if (index < 0) return false;
	state.hooks[index] = { ...state.hooks[index], last: run };
	return true;
}

const EVENT_LABELS: Record<HookInfo["event"], string> = {
	session_start: "Session start",
	user_prompt: "Your message",
	before_tool: "Before a tool",
	after_tool: "After a tool",
	stop: "Turn end",
};

export function hookEventLabel(event: HookInfo["event"]): string {
	return EVENT_LABELS[event];
}

/** The last run, in a few words: what it decided and how long it took. */
export function lastRunLabel(run: HookRun | null | undefined): string {
	if (!run) return "not run yet";
	const took = `${run.duration_ms} ms`;
	switch (run.result) {
		case "ok":
			return `ok · ${took}`;
		case "context":
			return `added context · ${took}`;
		case "block":
			return `blocked · ${took}`;
		case "ask":
			return `asked for approval · ${took}`;
		case "timeout":
			return `timed out · ${took}`;
		default:
			return `error${run.exit_code != null ? ` (exit ${run.exit_code})` : ""} · ${took}`;
	}
}

/** Why the workspace's hooks are not listed, or null when nothing to say. */
export function workspaceNote(state: HooksViewState): string | null {
	if (state.workspaceError) return `Workspace hooks set aside: ${state.workspaceError}`;
	if (state.workspaceState === "pending") {
		return "This workspace's config is not trusted yet, so its hooks do not run.";
	}
	if (state.workspaceState === "declined") {
		return "You declined this workspace's config, so its hooks do not run.";
	}
	return null;
}
