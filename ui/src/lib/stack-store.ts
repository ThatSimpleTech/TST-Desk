// Instruction-stack logic (TD-1201): applies `instruction_stack` events,
// asks the daemon for a fresh stack, derives display labels. Pure — the
// .svelte.ts wrapper injects the reactive state and the live connection.

import type {
	ClientMessageUnion,
	DaemonEventUnion,
	InstructionStackEntry,
} from "./protocol";

/** What the stack panel renders from. */
export interface StackState {
	/** Session these entries describe; null until the first stack lands. */
	sessionId: string | null;
	sources: InstructionStackEntry[];
	/** Prompt tokens summed over active sources only. */
	totalTokens: number;
	/** How the daemon counted tokens (e.g. "exact" or an estimate marker). */
	tokenMethod: string;
	/** Provider-observed cached prompt tokens on the last main-loop call.
	 *  null = no turn yet (or an older daemon) — unknown, not zero. */
	lastCachedTokens: number | null;
	/** False until the first stack for the session has landed. */
	loaded: boolean;
}

export function createStackState(): StackState {
	return {
		sessionId: null,
		sources: [],
		totalTokens: 0,
		tokenMethod: "",
		lastCachedTokens: null,
		loaded: false,
	};
}

export function clearStack(state: StackState): void {
	state.sessionId = null;
	state.sources = [];
	state.totalTokens = 0;
	state.tokenMethod = "";
	state.lastCachedTokens = null;
	state.loaded = false;
}

export interface StackStoreDeps {
	/** Send a client message; false when the socket is down. */
	send: (msg: ClientMessageUnion) => boolean;
}

export function createStackStore(deps: StackStoreDeps, state: StackState) {
	return {
		/** Ingest a daemon event; true when the stack was replaced. Between
		 *  explicit refreshes the loop pushes a fresh stack on every hot
		 *  reload (TD-509), so this is the live-update path. */
		applyEvent(event: DaemonEventUnion, currentSessionId: string | null): boolean {
			if (event.type !== "instruction_stack") return false;
			// The daemon can run several sessions; the panel shows only the
			// one the UI is attached to.
			if (event.session_id !== currentSessionId) return false;
			state.sessionId = event.session_id;
			state.sources = event.sources;
			state.totalTokens = event.total_tokens;
			state.tokenMethod = event.token_method;
			state.lastCachedTokens = event.last_cached_tokens ?? null;
			state.loaded = true;
			return true;
		},

		/** Ask the daemon for the attached session's stack (panel open,
		 *  session switch). A stale view from another session is dropped
		 *  first — an empty panel is honest, a stale one lies. */
		refresh(currentSessionId: string | null): boolean {
			if (currentSessionId === null) {
				clearStack(state);
				return false;
			}
			if (state.sessionId !== currentSessionId) clearStack(state);
			return deps.send({ type: "get_instruction_stack", session_id: currentSessionId });
		},

		clear(): void {
			clearStack(state);
		},
	};
}

export type StackStore = ReturnType<typeof createStackStore>;

/** Thousands-separated token count ("12,345"). */
export function formatTokens(tokens: number): string {
	return tokens.toLocaleString("en-US");
}

/** Cache badge text. Never implies a hit or miss the provider didn't
 *  report: before the first turn the honest answer is "unknown". */
export function cacheLabel(lastCachedTokens: number | null): string {
	if (lastCachedTokens === null) return "cache unknown until a turn runs";
	if (lastCachedTokens > 0) return `cached ${formatTokens(lastCachedTokens)} tokens`;
	return "cache miss";
}
