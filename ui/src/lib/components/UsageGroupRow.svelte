<script lang="ts">
	// One usage bucket and its per-tier split (TD-1706).
	//
	// Split out of UsagePanel so neither file crowds the ~400-line ceiling
	// (AGENTS §6). Purely presentational: every number here was computed by
	// the daemon from the audit store and folded by the pure helpers in
	// usage.ts — nothing is derived from what the UI happened to witness.
	import { formatBucketKey, formatTokens, rowTokens, type UsageBucket, type UsageGroup } from '../usage';
	import { formatUsd } from '../cost-format.js';

	interface Props {
		bucket: UsageBucket;
		group: UsageGroup;
	}
	let { bucket, group }: Props = $props();
</script>

<li class="group">
	<div class="group-head">
		<span class="group-key" title={group.key}>{formatBucketKey(bucket, group.key)}</span>
		<span class="group-figures">
			<span class="mono">{formatTokens(group.tokens)} tok</span>
			<span class="mono cost">{formatUsd(group.cost)}</span>
		</span>
	</div>
	<ul class="tiers">
		{#each group.rows as row (row.tier)}
			<li class="tier">
				<span class="tier-name">{row.tier}</span>
				<span class="mono" title="prompt / cached / completion">
					{formatTokens(row.prompt_tokens)} / {formatTokens(row.cached_prompt_tokens)} / {formatTokens(
						row.completion_tokens,
					)}
				</span>
				<span class="mono cost">{formatTokens(rowTokens(row))}</span>
				<span class="mono cost">{formatUsd(row.cost)}</span>
			</li>
		{/each}
	</ul>
	{#if group.classifierCost > 0}
		<!-- TD-703 keeps classifier spend off main-loop cost; folding it in
		     here would make this pane disagree with the title-bar meter. -->
		<p class="classifier">
			classifier <span class="mono">{formatUsd(group.classifierCost)}</span>
		</p>
	{/if}
</li>

<style>
	.group-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-3);
	}

	.group-key {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-text);
	}

	.group-figures {
		display: inline-flex;
		gap: var(--space-3);
		font-size: var(--text-xs);
	}

	.tiers {
		list-style: none;
		margin: var(--space-1) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.tier {
		display: grid;
		grid-template-columns: 5rem 1fr auto auto;
		align-items: baseline;
		gap: var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
	}

	.tier-name {
		font-weight: var(--weight-medium);
	}

	.mono {
		font-family: var(--font-mono);
	}

	.cost {
		color: var(--color-text);
	}

	.classifier {
		margin: var(--space-1) 0 0;
		font-size: var(--text-xs);
		color: var(--color-text-muted);
		display: flex;
		justify-content: space-between;
	}
</style>
