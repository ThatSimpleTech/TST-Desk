<script lang="ts">
	// Usage and cost pane (TD-1706).
	//
	// Presentational view over the rollups the daemon computed from the
	// audit store (TD-903 queries). Session / day / week buckets, each
	// broken out by tier, plus the JSONL and CSV exports — which are the
	// daemon's own TD-903 exporters, not a second serializer written here.
	//
	// Nothing on this screen is computed from spend the UI witnessed live:
	// the meter's running totals and this pane answer different questions,
	// and only the audit store can answer this one (AGENTS §6).
	import {
		usage,
		refreshUsage,
		selectBucket,
		exportUsage,
	} from '../usage.svelte.js';
	import { BUCKET_LABELS, USAGE_BUCKETS, formatTokens, groupByBucket, totalsFor } from '../usage';
	import { formatUsd } from '../cost-format.js';
	import { openInEditor } from '../open-file';
	import Icon from './Icon.svelte';
	import UsageGroupRow from './UsageGroupRow.svelte';

	let groups = $derived(groupByBucket(usage.rows, usage.bucket));
	let totals = $derived(totalsFor(groups));

	// The export path comes from the daemon's own data directory, never
	// from free text, so handing it to the OS opener is safe (TD-1201).
	function reveal(path: string): void {
		void openInEditor(path);
	}
</script>

<div class="usage-panel">
	<div class="toolbar">
		<div class="buckets" role="tablist" aria-label="Rollup period">
			{#each USAGE_BUCKETS as bucket (bucket)}
				<button
					role="tab"
					type="button"
					class="bucket"
					class:bucket-active={usage.bucket === bucket}
					aria-selected={usage.bucket === bucket}
					onclick={() => selectBucket(bucket)}
				>
					{BUCKET_LABELS[bucket]}
				</button>
			{/each}
		</div>
		<button
			class="action"
			type="button"
			title="Reload from the audit store"
			aria-label="Refresh usage"
			onclick={refreshUsage}
			disabled={usage.loading}><Icon name="clock" size={12} /></button
		>
	</div>

	{#if usage.error !== null}
		<p class="error" role="alert">{usage.error}</p>
	{/if}

	{#if usage.loading && !usage.loaded}
		<p class="empty">Reading the audit store…</p>
	{:else if groups.length === 0}
		<p class="empty">
			No recorded spend yet. Every model call this workspace makes is written to the audit
			store, and its token and dollar totals appear here by session, day, and week.
		</p>
	{:else}
		<div class="totals">
			<span class="totals-label">{groups.length}
				{groups.length === 1 ? BUCKET_LABELS[usage.bucket].toLowerCase() : `${BUCKET_LABELS[usage.bucket].toLowerCase()}s`}</span>
			<span class="totals-figures">
				<span class="tokens">{formatTokens(totals.tokens)} tok</span>
				<span class="cost">{formatUsd(totals.cost)}</span>
			</span>
		</div>

		<ul class="groups">
			{#each groups as group (group.key)}
				<UsageGroupRow bucket={usage.bucket} {group} />
			{/each}
		</ul>
	{/if}

	<div class="exports">
		<span class="exports-label">Export all model calls</span>
		<div class="exports-buttons">
			<button
				class="export"
				type="button"
				onclick={() => exportUsage('jsonl')}
				disabled={usage.exporting}>JSONL</button
			>
			<button
				class="export"
				type="button"
				onclick={() => exportUsage('csv')}
				disabled={usage.exporting}>CSV</button
			>
		</div>
		{#if usage.lastExport !== null}
			<button
				class="export-result"
				type="button"
				title={usage.lastExport.path}
				onclick={() => usage.lastExport && reveal(usage.lastExport.path)}
			>
				{usage.lastExport.rows}
				{usage.lastExport.rows === 1 ? 'call' : 'calls'} written to
				<span class="export-path">{usage.lastExport.path}</span>
			</button>
		{/if}
	</div>
</div>

<style>
	.usage-panel {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow-y: auto;
		padding: var(--space-3);
		gap: var(--space-3);
	}

	.toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
	}

	.buckets {
		display: inline-flex;
		gap: var(--space-1);
	}

	.bucket {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
		transition: border-color var(--transition-fast);
	}

	.bucket:hover {
		border-color: var(--color-accent);
	}

	.bucket-active {
		color: var(--color-accent-text);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.action {
		display: inline-flex;
		align-items: center;
		background: transparent;
		border: 0;
		color: var(--color-text-muted);
		padding: var(--space-1);
		border-radius: var(--radius-sm);
		cursor: pointer;
	}

	.action:hover:not(:disabled) {
		color: var(--color-text);
		background: var(--color-bg-subtle);
	}

	.action:disabled {
		cursor: default;
		opacity: 0.5;
	}

	.empty {
		margin: 0;
		padding: var(--space-4) var(--space-2);
		font-size: var(--text-xs);
		line-height: var(--leading-relaxed);
		color: var(--color-text-secondary);
	}

	.error {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-danger);
	}

	.totals {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-3);
		padding-bottom: var(--space-2);
		border-bottom: var(--border-width) solid var(--color-border);
	}

	.totals-label {
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
	}

	.totals-figures {
		display: inline-flex;
		gap: var(--space-3);
		font-size: var(--text-xs);
	}

	.groups {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.tokens,
	.cost {
		font-family: var(--font-mono);
	}

	.cost {
		color: var(--color-text);
	}

	.exports {
		margin-top: auto;
		padding-top: var(--space-3);
		border-top: var(--border-width) solid var(--color-border);
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.exports-label {
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
	}

	.exports-buttons {
		display: inline-flex;
		gap: var(--space-2);
	}

	.export {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		font-family: var(--font-mono);
		color: var(--color-text);
		background: transparent;
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.export:hover:not(:disabled) {
		border-color: var(--color-accent);
	}

	.export:disabled {
		cursor: default;
		opacity: 0.5;
	}

	.export-result {
		text-align: left;
		background: transparent;
		border: 0;
		padding: 0;
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
		cursor: pointer;
	}

	.export-result:hover {
		color: var(--color-text);
	}

	.export-path {
		font-family: var(--font-mono);
		word-break: break-all;
	}
</style>
