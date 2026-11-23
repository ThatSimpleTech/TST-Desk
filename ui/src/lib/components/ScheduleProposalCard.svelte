<script lang="ts">
	// Schedule proposal card (TD-5263): a job the agent proposed. Save, or
	// Edit then Save, is the only thing that creates it, and the daemon does
	// the write; Dismiss schedules nothing.
	import { onMount } from 'svelte';
	import { dismissProposal, forget, save } from '../schedule-proposal-store.svelte.js';
	import {
		nextRunsCopy,
		whenCopy,
		type PendingScheduleProposal,
		type ScheduleEdit,
	} from '../schedule-proposal';

	interface Props {
		proposal: PendingScheduleProposal;
	}

	let { proposal }: Props = $props();

	let cardEl: HTMLElement | null = null;
	let editing = $state<ScheduleEdit | null>(null);
	const draft = $derived(proposal.open ? editing : null);
	const oneOff = $derived(proposal.cadence === null);

	onMount(() => cardEl?.focus());

	function startEdit(): void {
		editing = { instruction: proposal.instruction, cadence: proposal.cadence ?? '' };
	}
</script>

<article class="card" tabindex="-1" bind:this={cardEl} aria-label="Scheduled job proposal">
	<header class="card-head">
		<span class="card-title">The agent proposes a scheduled job</span>
		<span class="badge">Proposed by the agent in this chat</span>
	</header>

	{#if draft === null}
		<p class="instruction">{proposal.instruction}</p>
		<dl class="facts">
			<dt>When</dt>
			<dd>{whenCopy(proposal)}</dd>
			{#if !oneOff && proposal.nextRuns.length > 0}
				<dt>Next runs</dt>
				<dd>{nextRunsCopy(proposal).join(' · ')}</dd>
			{/if}
			<dt>Workspace</dt>
			<dd class="path">{proposal.workspace}</dd>
		</dl>
	{:else}
		<label class="field">
			<span>What it should do</span>
			<textarea
				rows="4"
				value={draft.instruction}
				oninput={(e) => (editing = { ...draft, instruction: e.currentTarget.value })}
			></textarea>
		</label>
		{#if !oneOff}
			<label class="field">
				<span>Cadence</span>
				<input
					type="text"
					value={draft.cadence}
					oninput={(e) => (editing = { ...draft, cadence: e.currentTarget.value })}
				/>
			</label>
		{/if}
	{/if}

	{#if proposal.notice}
		<p class="notice" role="status">{proposal.notice}</p>
	{/if}

	<footer class="card-actions">
		{#if !proposal.open}
			<button class="btn btn--quiet" type="button" onclick={() => forget(proposal)}>Dismiss</button>
		{:else if draft === null}
			<button class="btn btn--accept" type="button" onclick={() => save(proposal)}>Save</button>
			<button class="btn btn--quiet" type="button" onclick={startEdit}>Edit</button>
			<button class="btn btn--reject" type="button" onclick={() => dismissProposal(proposal)}>
				Dismiss
			</button>
		{:else}
			<button class="btn btn--accept" type="button" onclick={() => save(proposal, draft)}>Save</button>
			<button class="btn btn--quiet" type="button" onclick={() => (editing = null)}>Cancel</button>
		{/if}
	</footer>
</article>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-left: var(--space-1) solid var(--color-accent);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-sm);
	}

	.card:focus {
		outline: 2px solid var(--color-accent);
		outline-offset: 2px;
	}

	.card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		flex-wrap: wrap;
	}

	.card-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.badge {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		padding: var(--space-1) var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		color: var(--color-ink-secondary);
	}

	.instruction {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}

	.facts {
		display: grid;
		grid-template-columns: auto 1fr;
		gap: var(--space-1) var(--space-3);
		margin: 0;
		font-size: var(--text-xs);
	}

	.facts dt {
		color: var(--color-ink-secondary);
	}

	.facts dd {
		margin: 0;
		color: var(--color-ink);
	}

	.path {
		overflow-wrap: anywhere;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.field textarea,
	.field input {
		font: inherit;
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-2);
	}

	.notice {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-warn);
	}

	.card-actions {
		display: flex;
		gap: var(--space-2);
	}

	.btn {
		font-size: var(--text-sm);
		padding: var(--space-1) var(--space-3);
		border-radius: var(--radius-sm);
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn--accept {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--quiet:hover,
	.btn--reject:hover {
		background: var(--color-lifted);
	}
</style>
