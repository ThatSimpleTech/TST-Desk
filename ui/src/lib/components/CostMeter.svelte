<script lang="ts">
	// Live cost meter and its hover breakdown (TD-1006, TD-1802, TD-1706).
	//
	// Split out of TitleBar so neither file crowds the ~400-line ceiling
	// (AGENTS §6). Presentational: every figure is reduced from daemon events
	// by session-status — the UI never derives spend it wasn't given.
	import { session } from '../session-status.svelte.js';
	import { formatUsd } from '../cost-format.js';
	import { openUsage } from '../usage.svelte.js';

	let costBreakdown = $derived(
		Object.entries(session.cost.byTier).sort((a, b) => b[1] - a[1]),
	);
</script>

<div class="meter">
	<button class="cost" type="button" title="Cost breakdown">
		{formatUsd(session.cost.session)}
	</button>
	<div class="popover">
		<p class="popover-title">Cost breakdown</p>
		<dl>
			<div><dt>This turn</dt><dd>{formatUsd(session.cost.turn)}</dd></div>
			{#each costBreakdown as [tier, cost] (tier)}
				<div><dt>{tier}</dt><dd>{formatUsd(cost)}</dd></div>
			{/each}
			{#if session.cost.classifier > 0}
				<div><dt>classifier</dt><dd>{formatUsd(session.cost.classifier)}</dd></div>
			{/if}
		</dl>
		<!-- The meter is this session's running spend; the usage pane
		     (TD-1706) is the audit store's history across all of them. -->
		<button class="popover-link" type="button" onclick={openUsage}>
			Usage and cost history →
		</button>
	</div>
</div>

<style>
	.meter {
		position: relative;
	}

	.cost {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text);
		font-family: var(--font-mono);
		background: transparent;
		border: 0;
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-md);
		cursor: default;
	}

	.popover {
		display: none;
		position: absolute;
		top: 100%;
		right: 0;
		z-index: 10;
		background: var(--color-bg-raised);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-lg);
		padding: var(--space-2) var(--space-3);
		white-space: nowrap;
	}

	.meter:hover .popover,
	.meter:focus-within .popover {
		display: block;
	}

	.popover-title {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		margin: 0 0 var(--space-1);
		font-family: var(--font-family);
	}

	.popover dl {
		margin: 0;
		font-family: var(--font-family);
	}

	.popover dl > div {
		display: flex;
		justify-content: space-between;
		gap: var(--space-4);
	}

	.popover dt {
		color: var(--color-text-secondary);
	}

	.popover dd {
		margin: 0;
		font-family: var(--font-mono);
	}

	.popover-link {
		display: block;
		width: 100%;
		margin-top: var(--space-2);
		padding-top: var(--space-2);
		border: 0;
		border-top: var(--border-width) solid var(--color-border);
		background: transparent;
		font-family: var(--font-family);
		font-size: var(--text-xs);
		color: var(--color-accent);
		text-align: left;
		cursor: pointer;
	}

	.popover-link:hover {
		color: var(--color-accent-hover);
	}
</style>
