<script lang="ts">
	// Chat pane (TD-1004, restyled TD-1604): conversation + composer held in
	// one centered column (max ≈760px) on wide windows. An empty conversation
	// shows the time-aware serif greeting with suggestion chips (TD-1605) —
	// hidden the moment messages exist, including attach/replay with history,
	// since both paths rebuild chat.messages through the same store. Cancel
	// lives in the composer's send→stop morph; Esc cancels too (TD-1609).
	// Presentational composition only — protocol state lives in the stores.
	import { onMount } from "svelte";
	import { cancelTurn, chat, initChat, retryLastUserMessage, sendUserMessage, teardownChat } from "../../chat-store.svelte.js";
	import { ws } from "../../connection-status.svelte.js";
	import { canSend, formatTurnDuration, showCancel } from "../../chat-store";
	import { greetingForHour, SUGGESTIONS } from "../../greeting";
	import Composer from "./Composer.svelte";
	import MessageList from "./MessageList.svelte";

	onMount(() => {
		initChat();
		return teardownChat;
	});

	// Computed once per mount; the greeting isn't meant to tick live as the
	// hour rolls over mid-session.
	const greeting = greetingForHour(new Date().getHours());

	// The draft lives here so chips insert text without touching the
	// composer's internals — insert, never auto-send.
	let draft = $state("");
</script>

<div class="chat-pane">
	<div class="column">
		{#if chat.messages.length === 0}
			<div class="empty" aria-label="Getting started">
				<p class="greeting">{greeting}.</p>
				<div class="chips" role="group" aria-label="Suggestions">
					{#each SUGGESTIONS as suggestion (suggestion)}
						<button class="chip" type="button" onclick={() => (draft = suggestion)}>
							{suggestion}
						</button>
					{/each}
				</div>
			</div>
		{:else}
			<MessageList
				messages={chat.messages}
				turnLive={showCancel(chat.turnState)}
				onretry={retryLastUserMessage}
			/>
		{/if}
		<!-- TD-1607: fixed-height slot — the shimmer and the duration line
		     swap without ever nudging the composer. -->
		<div class="turn-status" aria-live="polite">
			{#if chat.awaitingFirstToken}
				<span class="shimmer">Working…</span>
			{:else if chat.lastTurnDuration !== null}
				<span class="duration">Worked for {formatTurnDuration(chat.lastTurnDuration)}</span>
			{/if}
		</div>
		<Composer
			disabled={!canSend(chat.sessionId, ws.state)}
			running={showCancel(chat.turnState)}
			bind:value={draft}
			onsubmit={(text) => {
				sendUserMessage(text);
			}}
			oncancel={cancelTurn}
		/>
	</div>
</div>

<style>
	.chat-pane {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--color-ground);
	}

	.column {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		width: min(760px, 100%);
		margin-inline: auto;
	}

	.empty {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		justify-content: center;
		gap: var(--space-6);
		padding: 0 var(--space-4);
	}

	.greeting {
		margin: 0;
		font-family: var(--font-display);
		font-size: var(--text-3xl);
		font-weight: var(--weight-normal);
		letter-spacing: var(--tracking-display);
		line-height: var(--leading-tight);
		color: var(--color-ink);
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.chip {
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		cursor: pointer;
		transition:
			color var(--transition-fast),
			border-color var(--transition-fast);
	}

	.chip:hover {
		color: var(--color-ink);
		border-color: var(--color-ink-muted);
	}

	/* Reserved height: shimmer and duration swap inside the slot, so neither
	   ever moves the composer. */
	.turn-status {
		display: flex;
		align-items: center;
		height: calc(var(--space-6) + var(--space-2));
		padding: 0 var(--space-4);
		font-size: var(--text-sm);
	}

	.duration {
		color: var(--color-ink-muted);
	}

	/* Warm shimmer sweeping left to right via a moving gradient clipped to
	   the glyphs themselves. Reduced motion holds the edge color still. */
	.shimmer {
		background: linear-gradient(
			100deg,
			var(--color-ink-muted) 40%,
			var(--color-ink) 50%,
			var(--color-ink-muted) 60%
		);
		background-size: 200% 100%;
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
		animation: shimmer-sweep 1.6s linear infinite;
	}

	@keyframes shimmer-sweep {
		from {
			background-position: 100% 0;
		}
		to {
			background-position: -100% 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.shimmer {
			animation: none;
			background: none;
			color: var(--color-ink-muted);
		}
	}
</style>
