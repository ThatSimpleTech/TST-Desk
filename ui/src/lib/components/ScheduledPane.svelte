<script lang="ts">
	// Scheduled rail surface (TD-3805, TD-3810, TD-3811, TD-3813, TD-3814).
	//
	// Lists persisted jobs and creates, edits, pauses, deletes, and runs them
	// through protocol verbs. Draft fields, not NL. Run now fires one job; the
	// tick still owns the schedule. Edit loads the row; a cadence job's armed
	// slot stays off the form so Save does not send it back. History on a card
	// is that job's run log, not the single receipt on the row. A job can pin
	// a preset and an engine so it does not follow the window.
	import {
		cancelEdit,
		createJob,
		deleteScheduledJob,
		editJob,
		parseJobRequest,
		pauseJob,
		runJob,
		saveEdit,
		scheduled,
		setDraftField,
		setParseText,
	} from '../scheduled.svelte.js';
	import {
		jobActivity,
		jobFailed,
		jobFormCopy,
		jobMeta,
		jobMissed,
		jobsEmptyCopy,
		workspaceSuggestions,
	} from '../scheduled';
	import { visibleRecents, workspaces } from '../workspaces.svelte.js';
	import { workspaceName } from '../session-status.svelte.js';
	import EmptyState from './EmptyState.svelte';
	import ScheduledGraceField from './ScheduledGraceField.svelte';
	import ScheduledHistory from './ScheduledHistory.svelte';
	import ScheduledRetriesField from './ScheduledRetriesField.svelte';
	import ScheduledPinFields from './ScheduledPinFields.svelte';

	let empty = $derived(jobsEmptyCopy());
	let editing = $derived(scheduled.editingId !== null);
	let formCopy = $derived(jobFormCopy(editing));
	let known = $derived(
		workspaceSuggestions(
			workspaces.pinned,
			visibleRecents().map((r) => r.path),
		),
	);

	// Same native dialog the wizard and title bar use; a typed path still works.
	async function browse(): Promise<void> {
		const { open } = await import('@tauri-apps/plugin-dialog');
		const chosen = await open({ directory: true, multiple: false });
		if (typeof chosen === 'string' && chosen.length > 0) setDraftField('workspace', chosen);
	}
</script>

<div class="pane">
	<section class="list-col" aria-label="Scheduled">
		<h1 class="title">Scheduled</h1>
		<p class="lede">Jobs the daemon runs on a schedule. Run now fires one without moving its next slot.</p>
		{#if scheduled.items.length === 0}
			<EmptyState align="start" body={empty} />
		{:else}
			<ul class="list">
				{#each scheduled.items as row (row.id)}
					<li>
						<div class="card" class:card-failed={jobFailed(row)} class:card-missed={jobMissed(row)}>
							<span class="card-name">{row.instruction}</span>
							<span class="card-meta">{jobMeta(row)}</span>
							<span class="card-path">{row.workspace}</span>
							<span
								class="card-run"
								class:run-failed={jobFailed(row) && !row.running}
								class:run-missed={jobMissed(row) && !row.running}>{jobActivity(row)}</span
							>
							{#if row.last_summary}
								<p class="card-summary">{row.last_summary}</p>
							{/if}
							<ScheduledHistory jobId={row.id} />
							<div class="actions">
								<button class="action" type="button" onclick={() => editJob(row.id)}>Edit</button>
								<button
									class="action"
									type="button"
									disabled={row.running}
									onclick={() => runJob(row.id)}
								>
									Run now
								</button>
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
	<section class="form-col" aria-label={editing ? 'Edit scheduled job' : 'New scheduled job'}>
		<h2 class="form-title">{formCopy.title}</h2>
		<p class="lede">{formCopy.lede}</p>
		<label class="field">
			<span>Describe the job</span>
			<textarea
				rows="2"
				value={scheduled.parseText}
				oninput={(e) => setParseText(e.currentTarget.value)}
				placeholder="every 2 hours in /home/me/proj summarize the inbox deliver to slack"
			></textarea>
		</label>
		<button class="action" type="button" onclick={() => parseJobRequest()}>Parse</button>
		<label class="field">
			<span>Workspace</span>
			<span class="row">
				<input
					type="text"
					list="scheduled-workspaces"
					value={scheduled.draft.workspace}
					oninput={(e) => setDraftField('workspace', e.currentTarget.value)}
					placeholder="~/Documents/project"
				/>
				<button class="action" type="button" onclick={browse}>Browse…</button>
			</span>
			<datalist id="scheduled-workspaces">
				{#each known as path (path)}
					<option value={path} label={workspaceName(path)}></option>
				{/each}
			</datalist>
			<span class="hint">A full folder path, not a project name.</span>
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
				placeholder="weekdays at 7:45"
			/>
			<span class="hint">e.g. weekdays at 7:45 · every 2 hours · 45 7 * * 1-5</span>
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
		<ScheduledGraceField />
		<ScheduledRetriesField />
		<ScheduledPinFields />
		{#if scheduled.error !== null}
			<p class="error" role="alert">{scheduled.error}</p>
		{/if}
		<div class="row">
			<button class="action create" type="button" onclick={() => (editing ? saveEdit() : createJob())}>
				{formCopy.submit}
			</button>
			{#if editing}
				<button class="action" type="button" onclick={() => cancelEdit()}>Cancel</button>
			{/if}
		</div>
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

	/* Sits beside Create so a rejected save is read where it was made; the
	   daemon's message can carry a long path, so it must wrap. */
	.error {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-err);
		overflow-wrap: anywhere;
		white-space: pre-wrap;
	}

	.row {
		display: flex;
		gap: var(--space-2);
	}

	.row input {
		flex: 1;
		min-width: 0;
	}

	.hint {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
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
	.card-path,
	.card-run {
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
	}

	.card-run {
		margin-top: var(--space-1);
		color: var(--color-ink-muted);
	}

	.run-failed {
		color: var(--color-err);
	}

	.card-failed {
		border-color: var(--color-err);
	}

	/* Missed is a skipped slot, not a turn that failed. Warn, not the error red. */
	.run-missed {
		color: var(--color-warn);
	}

	.card-missed {
		border-color: var(--color-warn);
	}

	/* The last summary is the only place a scheduled run's output is
	   readable in the window; clamp it so one long turn cannot push the
	   rest of the list off-screen. */
	.card-summary {
		margin: var(--space-1) 0 0;
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

	.action:hover:not(:disabled) {
		background: var(--color-sunken);
	}

	.action:disabled {
		opacity: 0.5;
		cursor: default;
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
