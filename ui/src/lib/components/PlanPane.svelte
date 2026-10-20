<script lang="ts">
	import EmptyState from './EmptyState.svelte';
	import TaskListView from './TaskListView.svelte';
	import { approveGrokPlan, grok } from '../grok.svelte.js';
	import { staysOnCopy } from '../machines';
	import { machineNameFor } from '../machines.svelte.js';
	import { session } from '../session-status.svelte.js';
	import { progressLabel } from '../task-list';
	import { pinnedPlanFor, taskListFor } from '../task-list-store.svelte.js';
	import Markdown from './chat/Markdown.svelte';

	let comment = $state('');

	function approve(): void {
		if (session.sessionId === null) return;
		if (approveGrokPlan(session.sessionId, comment)) comment = '';
	}

	let hasPlan = $derived(grok.planMarkdown.length > 0 || grok.planEntries.length > 0);
	// TD-5015: Grok's plan for another computer's session stays there; the
	// plan held here is this computer's last one.
	let remoteName = $derived(machineNameFor(session.sessionId));
	// TD-5122: the native loop's task list. Display only, so another
	// computer's session shows its list too: the hub forwards the events.
	let tasks = $derived(taskListFor(session.sessionId));
	// TD-5123: the plan the person approved to end plan mode, pinned.
	let pinned = $derived(pinnedPlanFor(session.sessionId));
</script>

<div class="plan">
	{#if pinned !== '' || tasks.length > 0}
		{#if pinned !== ''}
			<section class="native" aria-label="Approved plan">
				<p class="progress">Approved plan · pinned</p>
				<div class="pinned"><Markdown text={pinned} /></div>
			</section>
		{/if}
		{#if tasks.length > 0}
			<section class="native" aria-label="Task list">
				<p class="progress">Tasks · {progressLabel(tasks)}</p>
				<TaskListView items={tasks} />
			</section>
		{/if}
	{:else if remoteName !== null}
		<EmptyState align="start" body={staysOnCopy(remoteName)} />
	{:else if !hasPlan}
		<EmptyState
			align="start"
			body="The agent's task list lands here as it works, and so does a plan you approve to end plan mode. In Grok's plan mode the plan lands here too: approve it to start implementing, or type a revision note first."
		/>
	{:else}
		{#if grok.planEntries.length > 0}
			<ol class="entries">
				{#each grok.planEntries as entry, i (i)}
					<li>
						<span class="status">{entry.status ?? 'pending'}</span>
						{entry.content}
					</li>
				{/each}
			</ol>
		{/if}
		{#if grok.planMarkdown}
			<pre class="md">{grok.planMarkdown}</pre>
		{/if}
		<div class="actions">
			<input
				class="note"
				type="text"
				placeholder="Optional revision note"
				bind:value={comment}
			/>
			<button type="button" class="go" onclick={approve}>Approve plan</button>
		</div>
	{/if}
</div>

<style>
	.plan {
		height: 100%;
		overflow-y: auto;
		padding: var(--space-3);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}
	.native {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}
	.pinned {
		font-size: var(--text-sm);
		overflow-wrap: anywhere;
	}
	.progress {
		margin: 0;
		color: var(--color-ink-muted);
		font-size: var(--text-sm);
	}
	.entries {
		margin: 0;
		padding-left: 1.2rem;
		font-size: var(--text-sm);
	}
	.status {
		color: var(--color-ink-muted);
		margin-right: 0.4rem;
		font-size: 0.75rem;
		text-transform: uppercase;
	}
	.md {
		white-space: pre-wrap;
		font-size: var(--text-sm);
		margin: 0;
		font-family: var(--font-mono);
	}
	.actions {
		display: flex;
		gap: var(--space-2);
	}
	.note {
		flex: 1;
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		color: inherit;
		padding: 6px 8px;
		border-radius: var(--radius-md);
	}
	.go {
		background: var(--color-accent);
		color: var(--color-on-accent);
		border: 0;
		border-radius: var(--radius-md);
		padding: 6px 10px;
		cursor: pointer;
	}
</style>
