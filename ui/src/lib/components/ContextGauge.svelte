<script lang="ts">
	// Context gauge (TD-5109): how full the window is for the next model
	// call, and a way to compact now.
	//
	// Presentational like CostMeter: every figure is the daemon's last
	// context_usage. Hidden until the loop has measured a request, so a
	// Grok session (which never sends one) shows nothing.
	import { session } from '../session-status.svelte.js';
	import { chat, requestCompact } from '../chat-store.svelte.js';
	import { showCancel } from '../chat-store';
	import { formatTokens, gaugeLabel, gaugeLevel, gaugePercent, gaugeTierLine } from '../context-gauge';

	let reading = $derived(session.context);
	let level = $derived(reading === null ? 'ok' : gaugeLevel(reading));
	let percent = $derived(reading === null ? 0 : gaugePercent(reading));
	// Where auto-compaction triggers, as a tick on the wide bar.
	let compactMark = $derived(
		reading === null || reading.context_window <= 0
			? 0
			: Math.min(100, Math.round((reading.compact_at / reading.context_window) * 100)),
	);
	// TD-5120: the reading names its own model, a backup's included.
	let tierLine = $derived(reading === null ? '' : gaugeTierLine(reading, session.modelSlugs));
	let onBackup = $derived(reading !== null && reading.fallback > 0);
	let busy = $derived(showCancel(chat.turnState));
</script>

{#if reading !== null}
	<div class="gauge gauge--{level}" class:gauge--backup={onBackup}>
		<button class="label" type="button" title="Context window" aria-haspopup="true">
			<span class="bar" aria-hidden="true">
				<span class="fill" style={`width: ${percent}%`}></span>
			</span>
			{gaugeLabel(reading)}
		</button>
		<div class="popover" role="group" aria-label="Context window">
			<p class="hero">
				<span class="hero-num">{percent}%</span>
				<span class="hero-sub">of the {formatTokens(reading.context_window)} window</span>
			</p>
			<div class="bar bar--wide" aria-hidden="true">
				<span class="fill" style={`width: ${percent}%`}></span>
				<span class="mark" style={`left: ${compactMark}%`}></span>
			</div>
			<dl class="rows">
				<div class="row">
					<dt>Next request</dt>
					<dd>{reading.used_tokens.toLocaleString()} tokens</dd>
				</div>
				<div class="row">
					<dt>Compacts by itself at</dt>
					<dd>{reading.compact_at.toLocaleString()}</dd>
				</div>
				<div class="row">
					<dt>Tier</dt>
					<dd>{tierLine}</dd>
				</div>
			</dl>
			{#if onBackup}
				<p class="hint hint--backup">
					The tier's own endpoint failed this turn, so these figures are the backup's. The next
					turn tries the tier's own model first.
				</p>
			{/if}
			<button
				class="popover-action"
				type="button"
				disabled={busy}
				title={busy ? 'Wait for the turn to finish, or stop it' : undefined}
				onclick={() => requestCompact()}
			>
				Compact now
			</button>
			<p class="hint">Or type <code>/compact</code> and what to keep.</p>
		</div>
	</div>
{/if}

<style>
	.gauge {
		position: relative;
	}

	.label {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 0;
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-md);
		cursor: default;
		white-space: nowrap;
	}

	.bar {
		position: relative;
		display: block;
		width: 40px;
		height: 4px;
		border-radius: var(--radius-full);
		background: var(--color-hairline);
		overflow: hidden;
	}

	.bar--wide {
		width: auto;
		margin-bottom: var(--space-3);
		overflow: visible;
	}

	.fill {
		display: block;
		height: 100%;
		border-radius: var(--radius-full);
		background: var(--color-accent);
		transition: width var(--transition-base);
	}

	.gauge--near .fill {
		background: var(--color-warn);
	}

	.gauge--over .fill {
		background: var(--color-err);
	}

	.mark {
		position: absolute;
		top: -3px;
		width: 2px;
		height: 10px;
		margin-left: -1px;
		background: var(--color-ink-muted);
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

	.gauge:hover .popover,
	.gauge:focus-within .popover {
		display: block;
	}

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

	.popover-action {
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

	.popover-action:hover:not(:disabled) {
		color: var(--color-accent-hover);
	}

	.popover-action:disabled {
		color: var(--color-ink-muted);
		cursor: not-allowed;
	}

	.hint {
		margin: var(--space-1) 0 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	/* TD-5120: a backup answered this turn. The popover is nowrap for its
	   figures; this sentence wraps at the popover's own width. */
	.hint--backup {
		white-space: normal;
		max-width: 15rem;
		margin-bottom: var(--space-2);
	}

	.gauge--backup .label {
		color: var(--color-warn);
	}
</style>
