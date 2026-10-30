<script lang="ts">
	// Chat pane (TD-1004, restyled TD-1604): conversation + composer held in
	// one centered column (max ≈760px) on wide windows. An empty conversation
	// shows the mascot (TD-5190) and the time-aware serif greeting with
	// suggestion chips (TD-1605) — hidden the moment messages exist,
	// including attach/replay with history, since both paths rebuild
	// chat.messages through the same store. Cancel
	// lives in the composer's send→stop morph; Esc cancels too (TD-1609).
	// Presentational composition only — protocol state lives in the stores:
	// the composer's attachment caps (TD-1709) come from the session store,
	// which reads them off boundary_update rather than inventing them.
	import { onMount } from "svelte";
	import {
		cancelTurn,
		chat,
		continueTurn,
		editQueuedMessage,
		initChat,
		keepDraft,
		removeHeldMessage,
		removeQueuedMessage,
		requestCompact,
		retryLastUserMessage,
		sendHeldMessage,
		sendQueuedNow,
		sendUserMessage,
		teardownChat,
	} from "../../chat-store.svelte.js";
	import { loadCommands, startCommands } from "../../commands.svelte.js";
	import { ws } from "../../connection-status.svelte.js";
	import { localWorkspacePath, session, setPlan, workspaceName } from "../../session-status.svelte.js";
	import { machineNameFor, sessionMachineUp } from "../../machines.svelte.js";
	import { unreachableNotice } from "../../machines";
	import { settings } from "../../settings.svelte.js";
	import { grok } from "../../grok.svelte.js";
	import { canSend, formatTurnDuration, showCancel } from "../../chat-store";
	import type { AttachmentDraft } from "../../attachments";
	import { parseCompactCommand } from "../../context-gauge";
	import { parsePlanCommand, type PlanCommand } from "../../plan-review";
	import { greetingContext, greetingForHour, SUGGESTIONS } from "../../greeting";
	import { workingVerb } from "../../working-flavor";
	import Composer from "./Composer.svelte";
	import MessageList from "./MessageList.svelte";
	import BackgroundHost from "../backgrounds/BackgroundHost.svelte";
	import HeldMessages from "./HeldMessages.svelte";
	import QueuedMessages from "./QueuedMessages.svelte";
	import TaskStrip from "./TaskStrip.svelte";
	import WorktreeNotice from "./WorktreeNotice.svelte";
	import { sessions } from "../../sessions.svelte.js";
	import { taskListFor } from "../../task-list-store.svelte.js";
	import GoalStrip from "./GoalStrip.svelte";
	import { goalFor } from "../../goal-store.svelte.js";
	import WakeupCard from "../WakeupCard.svelte";
	import Mascot from "../Mascot.svelte";
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
		// Slash commands are read from this computer's disk (TD-5015).
		const path = localWorkspacePath();
		if (path !== null) loadCommands(path);
	});

	// Another computer's session waits for that computer (TD-5015). Rows and
	// the transcript stay readable; only sending is held.
	const machineUp = $derived(sessionMachineUp(chat.sessionId));
	const machineName = $derived(machineNameFor(chat.sessionId));

	$effect(() => {
		bindWakeup(session.sessionId);
	});

	// Computed once per mount; the greeting isn't meant to tick live as the
	// hour rolls over mid-session.
	const greeting = greetingForHour(new Date().getHours());

	// The draft lives in the chat store so chips insert text without touching
	// the composer's internals — insert, never auto-send — and so it can be
	// kept per session through a restart (TD-5019).
	$effect(() => {
		keepDraft();
	});
	const sendable = $derived(canSend(chat.sessionId, ws.state, machineUp));
	// TD-5126: the bound chat's worktree, from the daemon's session list.
	const boundWorktree = $derived(
		sessions.rows.find((r) => r.sessionId === chat.sessionId)?.worktree ?? null,
	);

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

	// `/compact [what to keep]` compacts instead of reaching the model
	// (TD-5109), and `/plan` turns read-only plan mode on or off (TD-5123).
	// Grok sessions keep their own slash commands.
	function submitDraft(text: string, attachments: readonly AttachmentDraft[]): void {
		const grokEngine = session.engine === "grok";
		const plan = grokEngine ? null : parsePlanCommand(text);
		if (plan !== null && (plan.kind === "send" || attachments.length === 0)) {
			runPlanCommand(plan, attachments);
			return;
		}
		const compact = grokEngine || attachments.length > 0 ? null : parseCompactCommand(text);
		if (compact === null) {
			sendUserMessage(text, attachments);
			return;
		}
		requestCompact(compact.guidance);
	}

	function runPlanCommand(plan: PlanCommand, attachments: readonly AttachmentDraft[]): void {
		if (plan.kind === "toggle") {
			setPlan(!session.planMode);
		} else if (plan.kind === "set") {
			if (plan.on !== session.planMode) setPlan(plan.on);
		} else {
			// Plan mode first, on the same connection, so the task's turn plans.
			if (!session.planMode) setPlan(true);
			sendUserMessage(plan.text, attachments);
		}
	}

	// Engine this chat started with, falling back to Settings only when an
	// older daemon never named it. Grok fields are ignored unless they
	// belong to the focused session.
	const greetingEngine = $derived(session.engine ?? settings.engine);
	const grokForThisChat = $derived(grok.sessionId === session.sessionId);
	const contextLine = $derived(
		greetingContext({
			workspace:
				session.workspacePath !== null ? workspaceName(session.workspacePath) : null,
			engine: greetingEngine,
			tier: session.tier,
			slug: session.modelSlugs[session.tier] ?? null,
			grokMode: grokForThisChat ? grok.mode : null,
			grokModel: grokForThisChat ? grok.model : null,
		}),
	);
</script>

<div class="chat-pane">
	<!-- The picked background's chat-pane layer (TD-5202): Classic's header
	     light and dust (TD-1750), the veil behind the text, click ripples. -->
	<BackgroundHost layer="chat" />
	<div class="column">
		{#if chat.messages.length === 0}
			<div class="empty" aria-label="Getting started">
				<div class="hello">
					<!-- TD-5190: keyed on the session so every new chat gets the
					     entrance; asleep while nothing is live to answer. -->
					{#key chat.sessionId}
						<Mascot asleep={chat.sessionId === null} />
					{/key}
					<p class="greeting">{greeting}.</p>
					{#if chat.sessionId !== null && contextLine}
						<!-- Where you are and what will answer: workspace, engine,
						     and the model for this chat. -->
						<p class="context">{contextLine}</p>
					{/if}
				</div>
				{#if chat.sessionId === null}
					<!-- TD-1711: auto-bind refuses dead sessions, so a restart can
					     leave nothing live to bind. Point at the escape. -->
					<p class="pointer">Start a new session from the rail to begin.</p>
				{:else}
					{#if boundWorktree !== null}
						<WorktreeNotice sessionId={chat.sessionId} worktree={boundWorktree} />
					{/if}
					<div class="chips" role="group" aria-label="Suggestions">
						{#each SUGGESTIONS as suggestion (suggestion)}
							<button class="chip" type="button" onclick={() => (chat.draft = suggestion)}>
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
				<!-- TD-5159/TD-5183: the provider went quiet or cut the reply off
				     partway; the daemon withdrew it and is asking again. -->
				<span class="shimmer"
					>{chat.retryingReply ? "Reply interrupted. Asking again" : workingVerb(waitElapsedMs)}…</span
				>
				{#if waitElapsedMs >= 2000}
					<span class="elapsed">for {formatTurnDuration(waitElapsedMs / 1000)}</span>
				{/if}
			{:else if chat.lastTurnError === "interrupted_by_restart"}
				<!-- TD-5019: the daemon sealed a turn a restart cut off. Continue
				     is an ordinary message the user sends; nothing resumes alone. -->
				<span class="cut">Cut off by a restart</span>
				<button class="chip continue" type="button" disabled={!sendable} onclick={continueTurn}>
					Continue
				</button>
			{:else if chat.lastTurnDuration !== null}
				<span class="duration">Worked for {formatTurnDuration(chat.lastTurnDuration)}</span>
			{/if}
		</div>
		<!-- TD-5122: the agent's task list, the phone's view of the plan.
		     Renders nothing while there is no unfinished list. -->
		<!-- TD-5124: the goal /goal set; gone once a done-check meets it. -->
		<GoalStrip goal={goalFor(session.sessionId)} />
		<TaskStrip items={taskListFor(session.sessionId)} />
		<!-- TD-5155: what a restart left held, above the pane's own queue.
		     Renders nothing while nothing is held. -->
		<HeldMessages
			held={chat.held}
			disabled={!sendable}
			onsend={sendHeldMessage}
			onremove={removeHeldMessage}
		/>
		<!-- TD-1704: sits directly above the composer, between the turn status
		     and the card, so the rows read as "these go next". Renders nothing
		     at all while the queue is empty. -->
		<QueuedMessages
			queued={chat.queued}
			engine={session.engine}
			onsendnow={sendQueuedNow}
			onedit={editQueuedMessage}
			onremove={removeQueuedMessage}
		/>
		{#if !machineUp && machineName !== null}
			<p class="machine-notice" role="status">{unreachableNotice(machineName)}</p>
		{/if}
		<Composer
			disabled={!sendable}
			running={showCancel(chat.turnState) || chat.awaitingFirstToken || chat.reviewRunning}
			bind:value={chat.draft}
			limits={session.attachmentLimits}
			allowImages={session.vision}
			onsubmit={submitDraft}
			oncancel={cancelTurn}
		/>
	</div>
</div>

<style>
	.chat-pane {
		position: relative;
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		/* Transparent so the background shows through. AppShell's .pane-chat
		   wraps this pane and has to stay transparent too, or it paints the
		   page ground over the field. */
		background: transparent;
	}

	.column {
		position: relative;
		z-index: 1;
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

	.hello {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
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

	.context {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
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

	.chip:hover:not(:disabled) {
		color: var(--color-ink);
		border-color: var(--color-ink-muted);
	}

	/* Continue sits in the fixed-height turn-status slot, so it is a chip
	   with less vertical padding. */
	.continue {
		margin-left: var(--space-3);
		padding: var(--space-1) var(--space-3);
	}

	.continue:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.cut {
		color: var(--color-ink-secondary);
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

	.machine-notice {
		margin: 0;
		padding: var(--space-2) var(--space-4);
		font-size: var(--text-sm);
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
