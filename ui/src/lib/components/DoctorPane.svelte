<script lang="ts">
	// Doctor pane (TD-1104): renders the daemon's diagnostics_report as a row
	// list — one line per check with a status mark, detail, and (on failure)
	// the specific fix. "Copy report" puts the redacted plain-text version on
	// the clipboard. All daemon contact rides the doctor store.
	import { doctor, runDoctor, closeDoctor, copyDoctorReport } from '../doctor.svelte.js';
	import Icon from './Icon.svelte';
	import type { DiagnosticCheck } from '../protocol';

	/** Row status → glyph. The copied text report keeps its own ASCII marks
	    (STATUS_MARK in the store); these are the pane's visual analog. */
	const STATUS_ICON: Record<DiagnosticCheck['status'], 'check' | 'x' | 'minus'> = {
		ok: 'check',
		fail: 'x',
		skip: 'minus',
	};

	let copied = $state(false);
	let copiedTimer: ReturnType<typeof setTimeout> | null = null;

	async function copyReport(): Promise<void> {
		const ok = await copyDoctorReport();
		if (!ok) return;
		copied = true;
		if (copiedTimer !== null) clearTimeout(copiedTimer);
		copiedTimer = setTimeout(() => {
			copied = false;
		}, 2000);
	}
</script>

{#if doctor.open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div
		class="doctor-overlay"
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label="Doctor"
		onclick={(e) => {
			if (e.target === e.currentTarget) closeDoctor();
		}}
	>
		<div class="doctor">
			<div class="head">
				<h1 class="title">Doctor</h1>
				<button class="close" type="button" aria-label="Close" onclick={closeDoctor}>
					<Icon name="x" size={14} />
				</button>
			</div>

			<ul class="rows">
				{#each doctor.checks as row (row.name)}
					<li class="row">
						<span class="mark mark--{row.status}" aria-hidden="true"
							><Icon name={STATUS_ICON[row.status]} size={13} /></span
						>
						<div class="row-body">
							<span class="row-line">
								<span class="row-name">{row.name}</span><span class="row-detail">{row.detail}</span>
							</span>
							{#if row.fix}
								<span class="row-fix">fix: {row.fix}</span>
							{/if}
						</div>
					</li>
				{/each}
			</ul>
			{#if doctor.running}
				<p class="running" aria-live="polite">Running checks…</p>
			{/if}

			<div class="actions">
				<button class="btn btn--primary" type="button" onclick={runDoctor} disabled={doctor.running}>
					{doctor.running ? 'Running…' : 'Re-run'}
				</button>
				<button class="btn" type="button" onclick={copyReport} disabled={doctor.checks.length === 0}>
					{copied ? 'Copied' : 'Copy report'}
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.doctor-overlay {
		position: fixed;
		inset: 0;
		background: color-mix(in srgb, var(--color-bg) 78%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: var(--z-modal);
	}

	.doctor {
		width: min(38rem, calc(100vw - var(--space-8)));
		background: var(--color-bg-raised);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-xl);
		box-shadow: var(--shadow-lg);
		padding: var(--space-6);
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.title {
		margin: 0;
		font-family: var(--font-display);
		font-size: var(--text-xl);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-text);
	}

	.close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 24px;
		min-height: 24px;
		border: none;
		background: none;
		color: var(--color-text-secondary);
		cursor: pointer;
		font-size: var(--text-sm);
		padding: var(--space-2);
	}

	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		max-height: 20rem;
		overflow-y: auto;
	}

	.row {
		display: flex;
		gap: var(--space-2);
		align-items: baseline;
	}

	.mark {
		display: inline-flex;
		justify-content: center;
		width: 1.25rem;
		flex-shrink: 0;
	}

	.mark--ok {
		color: var(--color-success);
	}

	.mark--fail {
		color: var(--color-danger);
	}

	.mark--skip {
		color: var(--color-text-secondary);
	}

	.row-body {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
	}

	.row-line {
		font-size: var(--text-sm);
		color: var(--color-text);
	}

	.row-name {
		font-weight: var(--weight-medium);
		margin-right: var(--space-2);
	}

	.row-detail {
		color: var(--color-text-secondary);
	}

	.row-fix {
		font-size: var(--text-xs);
		color: var(--color-warning);
	}

	.running {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-text-secondary);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		justify-content: flex-end;
	}

	.btn {
		font-size: var(--text-sm);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		background: var(--color-bg);
		color: var(--color-text);
		cursor: pointer;
	}

	.btn--primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-bg);
	}

	.btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
</style>
