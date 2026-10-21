<script lang="ts">
	import EmptyState from './EmptyState.svelte';
	import TaskListView from './TaskListView.svelte';
	import { approveGrokPlan, grok } from '../grok.svelte.js';
	import { staysOnCopy } from '../machines';
	import { machineNameFor } from '../machines.svelte.js';
	import { session } from '../session-status.svelte.js';
	import { progressLabel } from '../task-list';
	import { pinnedPlanFor, taskListFor } from '../task-list-store.svelte.js';
	import { jobStateLabel, shellJobsFor } from '../shell-jobs-store.svelte.js';
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
	// TD-5137: the session's background shell jobs. The daemon owns them;
	// this only lists what its newest `shell_jobs` event says.
	let jobs = $derived(shellJobsFor(session.sessionId));
	let runningJobs = $derived(jobs.filter((job) => job.status === 'running').length);
</script>

<div class="plan">
	{#if jobs.length > 0}
		<section class="native" aria-label="Background jobs">
			<p class="progress">Background jobs · {runningJobs} running</p>
			<ul class="jobs">
				{#each jobs as job (job.job_id)}
					<li class="job">
						<span class="status">{jobStateLabel(job)}</span>
						<code class="command" title={job.command}>{job.command}</code>
						<span class="id">{job.job_id}</span>
					</li>
				{/each}
			</ul>
		</section>
	{/if}
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
		{#if jobs.length === 0}
			<EmptyState
				align="start"
				body="The agent's task list lands here as it works, and so does a plan you approve to end plan mode. In Grok's plan mode the plan lands here too: approve it to start implementing, or type a revision note first."
			/>
		{/if}
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
	.jobs {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-sm);
	}
	.job {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		min-width: 0;
	}
	.command {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono);
	}
	.id {
		color: var(--color-ink-muted);
		font-size: 0.75rem;
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
