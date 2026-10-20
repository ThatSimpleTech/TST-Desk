<script lang="ts">
	// Plan review card (TD-5123): the plan an `exit_plan_mode` call put up
	// at the end of read-only plan mode. Approve ends plan mode with the plan
	// pinned; Edit opens the plan as text, and Approve then sends the edit;
	// Reject keeps plan mode on. The note goes to the model either way. The
	// phone shows this same card, so the buttons are full-size tap targets.
	import { onMount, untrack } from 'svelte';
	import Icon from './Icon.svelte';
	import Markdown from './chat/Markdown.svelte';
	import { PLAN_MAX_CHARS } from '../plan-review';
	import { approvePlan, canReview, rejectPlan, type PlanCard } from '../plan-review-store.svelte.js';
	import { approvalWaitCopy } from '../machines';
	import { machineNameFor } from '../machines.svelte.js';

	interface Props {
		card: PlanCard;
	}

	let { card }: Props = $props();

	// The bar keys each card by its tool call id: one instance is one plan.
	let editing = $state(false);
	let draft = $state(untrack(() => card.plan));
	let feedback = $state('');
	let cardEl: HTMLElement | null = null;

	let waiting = $derived(canReview(card) ? null : approvalWaitCopy(machineNameFor(card.sessionId) ?? ''));
	let locked = $derived(card.sent || waiting !== null);
	let edited = $derived(draft.trim() !== card.plan.trim());
	let empty = $derived(draft.trim() === '');

	onMount(() => cardEl?.focus());

	function approve(): void {
		if (locked || empty) return;
		approvePlan(card, edited ? draft : null, feedback);
	}

	function reject(): void {
		if (!locked) rejectPlan(card, feedback);
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.defaultPrevented || event.key !== 'Escape') return;
		// Stops short of the shell's global handler, which would cancel the
		// turn behind the plan. Esc only closes the editor.
		event.preventDefault();
		event.stopPropagation();
		editing = false;
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<article
	class="card"
	tabindex="-1"
	bind:this={cardEl}
	data-plan-card={card.toolCallId}
	aria-label="The agent's plan"
	onkeydown={onKeydown}
>
	<header class="head">
		<span class="head-icon" aria-hidden="true"><Icon name="layers" size={16} /></span>
		<span class="title">The agent's plan</span>
		<span class="hint">Plan mode: nothing has changed yet</span>
	</header>

	{#if editing}
		<textarea
			class="editor"
			aria-label="Edit the plan"
			maxlength={PLAN_MAX_CHARS}
			disabled={locked}
			bind:value={draft}
		></textarea>
	{:else}
		<div class="plan"><Markdown text={draft} /></div>
	{/if}

	<input
		class="note"
		type="text"
		placeholder="Note for the agent (optional)"
		aria-label="Note for the agent"
		maxlength="4000"
		disabled={locked}
		bind:value={feedback}
	/>

	<footer class="actions">
		{#if card.notice !== null}<p class="notice" role="alert">{card.notice}</p>{/if}
		{#if waiting !== null}<p class="waiting" role="status">{waiting}</p>{/if}
		{#if card.sent}<p class="waiting" role="status">Sending your review…</p>{/if}
		<div class="buttons">
			<button class="btn btn--quiet" type="button" disabled={locked} onclick={reject}>Reject plan</button>
			<button
				class="btn btn--quiet"
				type="button"
				aria-pressed={editing}
				disabled={locked}
				onclick={() => (editing = !editing)}
			>
				<Icon name="pencil" size={14} />
				{editing ? 'Show plan' : 'Edit plan'}
			</button>
			<button class="btn btn--go" type="button" disabled={locked || empty} onclick={approve}>
				{edited ? 'Approve edited plan' : 'Approve plan'}
			</button>
		</div>
	</footer>
</article>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-left: var(--space-1) solid var(--color-accent);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-md);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.card:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 2px;
	}

	.head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2) var(--space-3);
	}

	.head-icon {
		display: inline-flex;
		color: var(--color-accent);
		flex-shrink: 0;
	}

	.title {
		font-size: var(--text-base);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

	.hint {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.plan {
		max-height: 40vh;
		overflow-y: auto;
		font-size: var(--text-sm);
		color: var(--color-ink);
		overflow-wrap: anywhere;
	}

	.editor,
	.note {
		font-size: var(--text-sm);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
		color: var(--color-ink);
	}

	.editor {
		min-height: 12rem;
		max-height: 40vh;
		resize: vertical;
		font-family: var(--font-mono);
	}

	.note::placeholder {
		color: var(--color-ink-muted);
	}

	.actions {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.notice {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-err);
	}

	.waiting {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.buttons {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		justify-content: flex-end;
	}

	.btn {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-4);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-md);
		cursor: pointer;
	}

	.btn:disabled {
		cursor: not-allowed;
		opacity: 0.6;
	}

	.btn--go {
		background: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--go:hover:not(:disabled) {
		background: var(--color-accent-hover);
	}

	.btn--quiet {
		background: transparent;
		color: var(--color-ink-secondary);
		border-color: var(--color-hairline);
	}

	/* On a phone each button is a full-width tap target. */
	@media (pointer: coarse), (max-width: 639px) {
		.buttons {
			flex-direction: column-reverse;
		}

		.btn {
			min-height: 44px;
			justify-content: center;
		}
	}
</style>
