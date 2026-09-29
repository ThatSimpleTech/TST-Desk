<script lang="ts">
	// Per-job run history (TD-3811).
	//
	// Opening asks for `list_job_runs`. A run that recorded a session uses the
	// rail's attach (`selectRow`) and returns to the chat, the same pair a
	// project recent uses. selectRow returns without a word when the id is
	// gone, so the missing case is said here instead.
	import { showHome } from '../projects.svelte.js';
	import { jobRunLabel, sessionMissingCopy } from '../scheduled';
	import { scheduled, toggleJobHistory } from '../scheduled.svelte.js';
	import { selectRow, sessions } from '../sessions.svelte.js';

	let { jobId }: { jobId: string } = $props();

	let open = $derived(scheduled.historyOpen[jobId] === true);
	let runs = $derived(scheduled.runs[jobId]);
	let missing = $state<Record<string, true>>({});

	function openSession(sessionId: string | null): void {
		if (sessionId === null || sessionId === '') return;
		const known = sessions.rows.some((row) => row.sessionId === sessionId);
		if (!known) {
			missing = { ...missing, [sessionId]: true };
			return;
		}
		selectRow(sessionId);
		showHome();
	}
</script>

<div class="history">
	<button
		class="disclosure"
		type="button"
		aria-expanded={open ? 'true' : 'false'}
		onclick={() => toggleJobHistory(jobId)}
	>
		History
	</button>
	{#if open}
		{#if runs === undefined}
			<p class="hint">Loading…</p>
		{:else if runs.length === 0}
			<p class="hint">No runs yet.</p>
		{:else}
			<ul class="runs">
				{#each runs as run, index (`${run.started_at}:${index}`)}
					<li class="run">
						<span class="when" class:failed={run.status === 'failed'}>{jobRunLabel(run)}</span>
						{#if run.summary}
							<p class="summary">{run.summary}</p>
						{/if}
						{#if run.session_id}
							{#if missing[run.session_id]}
								<p class="gone">{sessionMissingCopy()}</p>
							{:else}
								<button class="action" type="button" onclick={() => openSession(run.session_id)}>
									Open session
								</button>
							{/if}
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
</div>

<style>
	.history {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-2);
		margin-top: var(--space-2);
	}

	.disclosure,
	.action {
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		color: var(--color-ink);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-3);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.disclosure {
		background: transparent;
		color: var(--color-ink-secondary);
	}

	.disclosure:hover,
	.action:hover {
		background: var(--color-sunken);
	}

	.runs {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		width: 100%;
	}

	.run {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-1);
	}

	.when,
	.hint {
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-muted);
	}

	.failed,
	.gone {
		font-size: var(--text-xs);
		color: var(--color-err);
	}

	/* Same clamp as the row receipt: one long turn must not push the list away. */
	.summary {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
</style>
