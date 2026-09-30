<script lang="ts">
	// Local calendar that can block a regular slot (TD-3818). Presentational:
	// the draft store owns the path and the match list. Create omits blanks.
	// Save always sends them, including "" to clear. The dialog is the native
	// file picker, opened only when Browse is clicked.
	import { scheduled, setDraftField } from '../scheduled.svelte.js';

	async function browse(): Promise<void> {
		const { open } = await import('@tauri-apps/plugin-dialog');
		const chosen = await open({
			directory: false,
			multiple: false,
			title: 'Skip days in calendar',
			filters: [{ name: 'Calendar', extensions: ['ics'] }],
		});
		if (typeof chosen === 'string' && chosen.length > 0) {
			setDraftField('skip_calendar', chosen);
		}
	}
</script>

<div class="calendar">
	<label class="field">
		<span>Skip days in calendar</span>
		<span class="row">
			<input
				type="text"
				value={scheduled.draft.skip_calendar}
				oninput={(e) => setDraftField('skip_calendar', e.currentTarget.value)}
				placeholder="/Users/me/holidays.ics"
			/>
			<button class="action" type="button" onclick={browse}>Browse…</button>
		</span>
		<span class="hint">A local .ics file on this computer.</span>
	</label>
	<label class="field">
		<span>Only events matching</span>
		<input
			type="text"
			value={scheduled.draft.skip_match}
			oninput={(e) => setDraftField('skip_match', e.currentTarget.value)}
			placeholder="holiday|PTO|OOO"
		/>
		<span class="hint">Blank matches every event. holiday|PTO|OOO matches those words.</span>
	</label>
</div>

<style>
	.calendar {
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.row {
		display: flex;
		gap: var(--space-2);
	}

	.row input {
		flex: 1;
		min-width: 0;
	}

	.field input {
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
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

	.hint {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}
</style>
