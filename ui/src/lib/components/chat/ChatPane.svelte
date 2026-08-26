<script lang="ts">
	// Chat pane (TD-1004, restyled TD-1604): conversation + composer held in
	// one centered column (max ≈760px) on wide windows. An empty conversation
	// shows the time-aware serif greeting with suggestion chips (TD-1605) —
	// hidden the moment messages exist, including attach/replay with history,
	// since both paths rebuild chat.messages through the same store. Cancel
	// lives in the composer's send→stop morph; Esc cancels too (TD-1609).
	// Presentational composition only — protocol state lives in the stores:
	// the composer's attachment caps (TD-1709) come from the session store,
	// which reads them off boundary_update rather than inventing them.
	import { onMount } from "svelte";
	import {
		cancelTurn,
		chat,
		editQueuedMessage,
		initChat,
		removeQueuedMessage,
		retryLastUserMessage,
		sendQueuedNow,
		sendUserMessage,
		teardownChat,
	} from "../../chat-store.svelte.js";
	import { loadCommands, startCommands } from "../../commands.svelte.js";
	import { ws } from "../../connection-status.svelte.js";
	import { session } from "../../session-status.svelte.js";
	import { canSend, formatTurnDuration, showCancel } from "../../chat-store";
	import { greetingForHour, SUGGESTIONS } from "../../greeting";
	import { workingVerb } from "../../working-flavor";
	import Composer from "./Composer.svelte";
	import MessageList from "./MessageList.svelte";
	import QueuedMessages from "./QueuedMessages.svelte";
	import WakeupCard from "../WakeupCard.svelte";
	import { bindWakeup, startWakeup } from "../../wakeup.svelte.js";

	onMount(() => {
		initChat();
		const offWakeup = startWakeup();
		const offCommands = startCommands();
		return () => {
			teardownChat();
			offWakeup();
			offCommands();
		};
	});

	$effect(() => {
		const path = session.workspacePath;
		if (path !== null) loadCommands(path);
	});

	$effect(() => {
		bindWakeup(session.sessionId);
	});

	// Computed once per mount; the greeting isn't meant to tick live as the
	// hour rolls over mid-session.
	const greeting = greetingForHour(new Date().getHours());

	// The draft lives here so chips insert text without touching the
	// composer's internals — insert, never auto-send.
	let draft = $state("");

	// Working-line clock (TD-1713): ticks only while a first-token wait is in
	// flight, so the verb rotation and elapsed readout cost nothing at rest.
	let now = $state(Date.now());
	$effect(() => {
		if (!chat.awaitingFirstToken || chat.awaitingSince === null) return;
		now = Date.now();
		const tick = setInterval(() => {
			now = Date.now();
		}, 500);
		return () => clearInterval(tick);
	});
	const waitElapsedMs = $derived(chat.awaitingSince === null ? 0 : Math.max(0, now - chat.awaitingSince));
</script>

<div class="chat-pane">
	<div class="column">
		{#if chat.messages.length === 0}
			<div class="empty" aria-label="Getting started">
				<p class="greeting">{greeting}.</p>
				{#if chat.sessionId === null}
					<!-- TD-1711: auto-bind refuses dead sessions, so a restart can
					     leave nothing live to bind. Point at the escape. -->
					<p class="pointer">Start a new session from the rail to begin.</p>
				{:else}
					<div class="chips" role="group" aria-label="Suggestions">
						{#each SUGGESTIONS as suggestion (suggestion)}
							<button class="chip" type="button" onclick={() => (draft = suggestion)}>
								{suggestion}
							</button>
						{/each}
					</div>
				{/if}
			</div>
		{:else}
			<MessageList
				messages={chat.messages}
				turnLive={showCancel(chat.turnState)}
				onretry={retryLastUserMessage}
			/>
		{/if}
		<WakeupCard />
		<!-- TD-1607: fixed-height slot — the shimmer and the duration line
		     swap without ever nudging the composer. -->
		<div class="turn-status" aria-live="polite">
			{#if chat.turnStalled}
				<!-- TD-1713: 25s with no first token — stop shimmering, say what
				     we know. Cancel stays live below; the first delta recovers this. -->
				<span class="stalled">No response yet — the model may be slow or unreachable.</span>
			{:else if chat.awaitingFirstToken}
				<span class="shimmer">{workingVerb(waitElapsedMs)}…</span>
				{#if waitElapsedMs >= 2000}
					<span class="elapsed">for {formatTurnDuration(waitElapsedMs / 1000)}</span>
				{/if}
			{:else if chat.lastTurnDuration !== null}
				<span class="duration">Worked for {formatTurnDuration(chat.lastTurnDuration)}</span>
			{/if}
		</div>
		<!-- TD-1704: sits directly above the composer, between the turn status
		     and the card, so the rows read as "these go next". Renders nothing
		     at all while the queue is empty. -->
		<QueuedMessages
			queued={chat.queued}
			onsendnow={sendQueuedNow}
			onedit={editQueuedMessage}
			onremove={removeQueuedMessage}
		/>
		<Composer
			disabled={!canSend(chat.sessionId, ws.state)}
			running={showCancel(chat.turnState) || chat.awaitingFirstToken}
			bind:value={draft}
			limits={session.attachmentLimits}
			onsubmit={(text, attachments) => {
				sendUserMessage(text, attachments);
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

	.pointer {
		margin: 0;
		font-size: var(--text-base);
		color: var(--color-ink-secondary);
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

	/* The elapsed tail of the Working line — quieter than the verb itself. */
	.elapsed {
		margin-left: var(--space-2);
		color: var(--color-ink-muted);
	}

	/* Stall honesty (TD-1713): static and secondary — a report, not an alarm. */
	.stalled {
		color: var(--color-ink-secondary);
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
