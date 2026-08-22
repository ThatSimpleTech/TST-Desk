<script lang="ts">
	// Scheduled rail surface (TD-3805).
	//
	// Lists persisted jobs and creates / pauses / deletes them through
	// protocol verbs. Draft fields, not NL. The pane never runs a job.
	import {
		createJob,
		deleteScheduledJob,
		pauseJob,
		scheduled,
		setDraftField,
	} from '../scheduled.svelte.js';
	import { jobsEmptyCopy, jobWhen } from '../scheduled';

	let empty = $derived(jobsEmptyCopy());
</script>

<div class="pane">
	<section class="list-col" aria-label="Scheduled">
		<h1 class="title">Scheduled</h1>
		<p class="lede">Jobs the daemon will run. This pane does not fire them.</p>
		{#if scheduled.error !== null}
			<p class="error">{scheduled.error}</p>
		{/if}
		{#if scheduled.items.length === 0}
			<p class="empty">{empty}</p>
		{:else}
			<ul class="list">
				{#each scheduled.items as row (row.id)}
					<li>
						<div class="card">
							<span class="card-name">{row.instruction}</span>
							<span class="card-meta">{jobWhen(row)} · {row.deliver_to}</span>
							<span class="card-path">{row.workspace}</span>
							<div class="actions">
								<button class="action" type="button" onclick={() => pauseJob(row.id)}>
									{row.paused ? 'Resume' : 'Pause'}
								</button>
								<button class="action action-danger" type="button" onclick={() => deleteScheduledJob(row.id)}>
									Delete
								</button>
							</div>
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</section>
	<section class="form-col" aria-label="New scheduled job">
		<h2 class="form-title">New job</h2>
		<p class="lede">Draft fields. Cadence or next run, not both.</p>
		<label class="field">
			<span>Workspace</span>
			<input
				type="text"
				value={scheduled.draft.workspace}
				oninput={(e) => setDraftField('workspace', e.currentTarget.value)}
			/>
		</label>
		<label class="field">
			<span>Instruction</span>
			<textarea
				rows="3"
				value={scheduled.draft.instruction}
				oninput={(e) => setDraftField('instruction', e.currentTarget.value)}
			></textarea>
		</label>
		<label class="field">
			<span>Cadence</span>
			<input
				type="text"
				value={scheduled.draft.cadence}
				oninput={(e) => setDraftField('cadence', e.currentTarget.value)}
			/>
		</label>
		<label class="field">
			<span>Next run</span>
			<input
				type="text"
				value={scheduled.draft.next_run}
				oninput={(e) => setDraftField('next_run', e.currentTarget.value)}
			/>
		</label>
		<label class="field">
			<span>Deliver to</span>
			<select
				value={scheduled.draft.deliver_to}
				onchange={(e) =>
					setDraftField('deliver_to', e.currentTarget.value as typeof scheduled.draft.deliver_to)}
			>
				<option value="window">window</option>
				<option value="slack">slack</option>
				<option value="ntfy">ntfy</option>
			</select>
		</label>
		<button class="action create" type="button" onclick={() => createJob()}>Create</button>
	</section>
</div>

<style>
	.pane {
		display: flex;
		height: 100%;
		min-height: 0;
		background: var(--color-ground);
	}

	.list-col,
	.form-col {
		min-width: 0;
		min-height: 0;
		overflow: auto;
	}

	.list-col {
		flex: 1;
		padding: var(--space-8) var(--space-6);
		border-right: var(--border-width) solid var(--color-hairline);
	}

	.form-col {
		flex: 0 0 20rem;
		padding: var(--space-8) var(--space-6);
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.title,
	.form-title {
		margin: 0;
		font-family: var(--font-display);
		font-weight: var(--weight-normal);
		letter-spacing: var(--tracking-display);
		line-height: var(--leading-tight);
		color: var(--color-ink);
	}

	.title {
		font-size: var(--text-3xl);
	}

	.form-title {
		font-size: var(--text-xl);
		font-weight: var(--weight-medium);
	}

	.lede {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.empty {
		margin: var(--space-8) 0 0;
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
	}

	.error {
		margin: var(--space-4) 0 0;
		font-size: var(--text-sm);
		color: var(--color-err);
	}

	.list {
		list-style: none;
		margin: var(--space-6) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.card {
		display: flex;
		flex-direction: column;
		gap: 1px;
		width: 100%;
		text-align: left;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		padding: var(--space-3) var(--space-4);
		color: var(--color-ink);
	}

	.card-name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
	}

	.card-meta,
	.card-path {
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		margin-top: var(--space-2);
	}

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

	.action:hover {
		background: var(--color-sunken);
	}

	.action-danger {
		color: var(--color-err);
	}

	.create {
		align-self: flex-start;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.field input,
	.field textarea,
	.field select {
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
	}
</style>
