<script lang="ts">
	// Decisions-ledger panel (TD-1202): the session's decisions as a dense,
	// scannable list — class chip, choice, short commit — one line per row so
	// a Class A pass takes seconds, not minutes (AC #4). Click expands for the
	// rationale and a copyable revert command. The footer links out to the
	// workspace ledger file via the shell's open_path command.
	import {
		decisions,
		closeDecisions,
		setClassFilter,
		filteredDecisions,
		ledgerPath,
		type DecisionRow,
	} from '../decisions.svelte.js';
	import Icon from './Icon.svelte';
	import EmptyState from './EmptyState.svelte';

	let expandedId = $state<string | null>(null);
	let copiedId = $state<string | null>(null);

	const rows = $derived(filteredDecisions());
	const ledger = $derived(ledgerPath());

	const CLASS_FILTERS: Array<{ key: 'A' | 'B' | 'C' | null; label: string }> = [
		{ key: 'A', label: 'A' },
		{ key: 'B', label: 'B' },
		{ key: 'C', label: 'C' },
		{ key: null, label: 'All' },
	];

	function toggle(row: DecisionRow): void {
		expandedId = expandedId === row.id ? null : row.id;
	}

	function shortSha(commit: string): string {
		return commit.slice(0, 7);
	}

	async function copyText(text: string, id: string): Promise<void> {
		try {
			await navigator.clipboard.writeText(text);
			copiedId = id;
			setTimeout(() => {
				if (copiedId === id) copiedId = null;
			}, 2000);
		} catch {
			// Clipboard unavailable — nothing to offer.
		}
	}

	async function openLedger(): Promise<void> {
		if (ledger === null) return;
		try {
			const { invoke } = await import('@tauri-apps/api/core');
			await invoke('open_path', { path: ledger });
		} catch {
			// No shell (dev browser) — the path is the fallback.
			await copyText(ledger, 'ledger');
		}
	}
</script>

{#if decisions.open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div
		class="decisions-overlay"
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label="Decisions"
		onclick={(e) => {
			if (e.target === e.currentTarget) closeDecisions();
		}}
	>
		<div class="decisions">
			<div class="head">
				<h1 class="title">Decisions</h1>
				<div class="filters" role="group" aria-label="Filter by class">
					{#each CLASS_FILTERS as f}
						<button
							class="chip"
							class:chip--on={decisions.classFilter === f.key}
							type="button"
							onclick={() => setClassFilter(f.key)}>{f.label}</button
						>
					{/each}
				</div>
				<button class="close" type="button" aria-label="Close" onclick={closeDecisions}>
					<Icon name="x" size={14} />
				</button>
			</div>

			{#if rows.length === 0}
				<EmptyState align="start" body="No decisions logged for this session yet." />
			{:else}
				<ul class="rows" role="list">
					{#each rows as row (row.id)}
						<li class="row">
							<button class="row-line" type="button" onclick={() => toggle(row)}>
								<span class="class-chip class-{row.decisionClass.toLowerCase()}"
									>{row.decisionClass}</span
								>
								<span class="what">{row.what}</span>
								{#if row.commit}
									<span class="sha">{shortSha(row.commit)}</span>
								{/if}
							</button>
							{#if expandedId === row.id}
								<div class="row-detail">
									<p class="why">{row.why}</p>
									{#if row.undoCommand}
										<button
											class="btn btn--ghost"
											type="button"
											onclick={() => copyText(row.undoCommand!, row.id)}
											>{copiedId === row.id ? 'Copied' : `Copy revert: ${row.undoCommand}`}</button
										>
									{:else}
										<span class="no-undo">No revert — not attributed to a commit.</span>
									{/if}
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}

			{#if ledger !== null}
				<div class="foot">
					<button class="link" type="button" onclick={openLedger}>
						{copiedId === 'ledger' ? 'Path copied' : 'Open the full ledger'}
					</button>
					<span class="path" title={ledger}>{ledger}</span>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.decisions-overlay {
		position: fixed;
		inset: 0;
		background: color-mix(in srgb, var(--color-ground) 78%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: var(--z-modal);
	}

	.decisions {
		width: min(40rem, calc(100vw - var(--space-8)));
		max-height: 80vh;
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
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
		gap: var(--space-3);
	}

	.title {
		margin: 0;
		font-family: var(--font-display);
		font-size: var(--text-xl);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-ink);
	}

	.filters {
		display: flex;
		gap: var(--space-1);
		flex: 1;
	}

	.chip {
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		background: transparent;
		color: var(--color-ink-secondary);
		font-size: var(--text-xs);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
	}

	.chip--on {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-on-accent);
	}

	.close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 24px;
		min-height: 24px;
		border: none;
		background: none;
		color: var(--color-ink-secondary);
		cursor: pointer;
		font-size: var(--text-sm);
		padding: var(--space-2);
	}

	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}

	.row-line {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		width: 100%;
		border: none;
		background: none;
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		text-align: left;
		border-radius: var(--radius-sm);
	}

	.row-line:hover {
		background: var(--color-sunken);
	}

	.class-chip {
		flex-shrink: 0;
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		width: 1rem;
		text-align: center;
	}

	.class-a {
		color: var(--color-err);
	}

	.class-b {
		color: var(--color-warn);
	}

	.class-c {
		color: var(--color-ok);
	}

	.what {
		font-size: var(--text-sm);
		color: var(--color-ink);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		flex: 1;
	}

	.sha {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		flex-shrink: 0;
	}

	.row-detail {
		padding: var(--space-2) var(--space-2) var(--space-2) calc(1rem + var(--space-4));
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.why {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: 1.5;
	}

	.btn--ghost {
		align-self: flex-start;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: transparent;
		color: var(--color-ink);
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
	}

	.no-undo {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.foot {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		border-top: var(--border-width) solid var(--color-hairline);
		padding-top: var(--space-3);
	}

	.link {
		border: none;
		background: none;
		color: var(--color-accent);
		font-size: var(--text-sm);
		cursor: pointer;
		padding: 0;
		flex-shrink: 0;
	}

	.path {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
