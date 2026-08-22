// Instruction-stack logic (TD-1201): applies `instruction_stack` events,
// asks the daemon for a fresh stack, derives display labels. Pure — the
// .svelte.ts wrapper injects the reactive state and the live connection.

import type {
	ClientMessageUnion,
	DaemonEventUnion,
	InstructionStackEntry,
	MemoryStackEntry,
	SkillStackEntry,
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
	/** Cached prompt tokens the provider reported on the last main-loop
	 *  call. null = no figure was reported — unknown, not zero. */
	lastCachedTokens: number | null;
	/** Whether a main-loop call has come back at all. Separates the two
	 *  reasons lastCachedTokens is null: no turn yet, versus a provider
	 *  that reports no cache figure (TD-1811). */
	cacheObserved: boolean;
	/** False until the first stack for the session has landed. */
	loaded: boolean;
	memory: MemoryStackEntry[];
	memoryDropped: MemoryStackEntry[];
	memoryPlaceholder: boolean;
	/** Skill bodies loaded so far (TD-4502), newest load per name. */
	skills: SkillStackEntry[];
}

export function createStackState(): StackState {
	return {
		sessionId: null,
		sources: [],
		totalTokens: 0,
		tokenMethod: "",
		lastCachedTokens: null,
		cacheObserved: false,
		loaded: false,
		memory: [],
		memoryDropped: [],
		memoryPlaceholder: true,
		skills: [],
	};
}

export function clearStack(state: StackState): void {
	state.sessionId = null;
	state.sources = [];
	state.totalTokens = 0;
	state.tokenMethod = "";
	state.lastCachedTokens = null;
	state.cacheObserved = false;
	state.loaded = false;
	state.memory = [];
	state.memoryDropped = [];
	state.memoryPlaceholder = true;
	state.skills = [];
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
			// An older daemon omits the field; false is the honest read of
			// "we were not told that a call has landed".
			state.cacheObserved = event.cache_observed ?? false;
			state.memory = event.memory ?? [];
			state.memoryDropped = event.memory_dropped ?? [];
			state.memoryPlaceholder = event.memory_placeholder ?? true;
			// Older daemons omit the field; an empty list is the honest read.
			state.skills = event.skills ?? [];
			state.loaded = true;
			return true;
		},

		/** Ask the daemon for the attached session's stack (panel open,
		 *  session switch). A stale view from another session is dropped
		 *  first — an empty panel is honest, a stale one lies.
		 *
		 *  TD-1204: this only helps if a subscriber is already applying
		 *  `instruction_stack` events. The wrapper's `startStack` must
		 *  run before this, or the reply is dropped. */
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

/** Honest empty-memory copy — the same sentence the prompt carries. */
export function memoryPlaceholderCopy(): string {
	return "<!-- memory: none loaded for this session -->";
}

export function memoryReasonLabel(reason: MemoryStackEntry["reason"]): string {
	if (reason === "always-index") return "always-index";
	if (reason === "embedding") return "embedding";
	return "heading";
}

/** Which of the four things the badge can honestly say (TD-1811). A miss
 *  is a provider's report of zero reuse; a provider that reports nothing
 *  has not reported a miss, and the two must not render alike. */
export type CacheBadge = "unobserved" | "unreported" | "miss" | "hit";

export function cacheBadge(
	lastCachedTokens: number | null,
	cacheObserved: boolean,
): CacheBadge {
	if (!cacheObserved) return "unobserved";
	if (lastCachedTokens === null) return "unreported";
	return lastCachedTokens > 0 ? "hit" : "miss";
}

/** Cache badge text. Never implies a hit or miss the provider didn't
 *  report: before the first turn, and on a provider that reports no cache
 *  figure, the honest answer says so instead of naming a number. */
export function cacheLabel(lastCachedTokens: number | null, cacheObserved: boolean): string {
	switch (cacheBadge(lastCachedTokens, cacheObserved)) {
		case "unobserved":
			return "cache unknown until a turn runs";
		case "unreported":
			return "provider reports no cache figure";
		case "hit":
			return `cached ${formatTokens(lastCachedTokens as number)} tokens`;
		case "miss":
			return "cache miss";
	}
}
