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
	let cap = $derived(session.boundary?.spend_usd ?? 0);
	let ratio = $derived(cap > 0 ? Math.min(1, session.cost.session / cap) : 0);
	// Per-tier bars are relative to the largest tier, so the shape of the
	// spend reads at a glance even when every number is small.
	let largest = $derived(costBreakdown[0]?.[1] ?? 0);
</script>

<div class="meter">
	<button class="cost" type="button" title="Cost breakdown" aria-haspopup="true">
		{formatUsd(session.cost.session)}
	</button>
	<div class="popover" role="group" aria-label="Cost breakdown">
		<p class="hero">
			<span class="hero-num">{formatUsd(session.cost.session)}</span>
			<span class="hero-sub">this session{cap > 0 ? ` · ${formatUsd(cap)} cap` : ''}</span>
		</p>
		{#if cap > 0}
			<div class="bar" aria-hidden="true">
				<span class="fill" style={`width: ${Math.round(ratio * 100)}%`}></span>
			</div>
		{/if}
		<dl class="rows">
			<div class="row"><dt>This turn</dt><dd>{formatUsd(session.cost.turn)}</dd></div>
			{#each costBreakdown as [tier, cost] (tier)}
				<div class="row row--tier">
					<dt>{tier}</dt>
					<dd>{formatUsd(cost)}</dd>
					<dd class="tier-bar" aria-hidden="true">
						<span class="tier-fill" style={`width: ${largest > 0 ? Math.round((cost / largest) * 100) : 0}%`}></span>
					</dd>
				</div>
			{/each}
			{#if session.cost.classifier > 0}
				<div class="row"><dt>classifier</dt><dd>{formatUsd(session.cost.classifier)}</dd></div>
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
		color: var(--color-ink);
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
		min-width: 15rem;
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-lg);
		padding: var(--space-3);
		white-space: nowrap;
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.meter:hover .popover,
	.meter:focus-within .popover {
		display: block;
	}

	/* The number is the point of the popover; it gets the display face. */
	.hero {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		margin: 0 0 var(--space-2);
	}

	.hero-num {
		font-family: var(--font-display);
		font-size: var(--text-xl);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-ink);
	}

	.hero-sub {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.bar {
		height: 4px;
		margin-bottom: var(--space-3);
		border-radius: var(--radius-full);
		background: var(--color-hairline);
		overflow: hidden;
	}

	.fill {
		display: block;
		height: 100%;
		border-radius: var(--radius-full);
		background: var(--color-accent);
	}

	.rows {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		font-size: var(--text-xs);
	}

	.row {
		display: grid;
		grid-template-columns: 1fr auto;
		column-gap: var(--space-4);
		align-items: baseline;
	}

	.row dt {
		color: var(--color-ink-secondary);
	}

	.row dd {
		margin: 0;
		font-family: var(--font-mono);
		color: var(--color-ink);
	}

	.tier-bar {
		grid-column: 1 / -1;
		height: 3px;
		border-radius: var(--radius-full);
		background: var(--color-sunken);
		overflow: hidden;
	}

	.tier-fill {
		display: block;
		height: 100%;
		border-radius: var(--radius-full);
		background: var(--color-ink-muted);
	}

	.popover-link {
		display: block;
		width: 100%;
		margin-top: var(--space-3);
		padding-top: var(--space-2);
		border: 0;
		border-top: var(--border-width) solid var(--color-hairline);
		background: transparent;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-accent);
		text-align: left;
		cursor: pointer;
	}

	.popover-link:hover {
		color: var(--color-accent-hover);
	}
</style>
