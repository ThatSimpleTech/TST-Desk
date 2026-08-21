<script lang="ts">
	// Title bar (TD-1006): workspace picker, tier chips, live cost meter,
	// session state indicator, and the boundary ("wall") summary. Presentational
	// only — everything shown is reduced from daemon events via session-status;
	// the UI never derives truth it wasn't given (AGENTS §6).
	//
	// The picker and the meter own their own markup and styles in
	// WorkspacePicker/CostMeter; what stays here is the row itself.
	import { session, setTier, setPlanMode, type SessionIndicator } from '../session-status.svelte.js';
	import { formatUsd } from '../cost-format.js';
	import CostMeter from './CostMeter.svelte';
	import CuKillSwitch from './CuKillSwitch.svelte';
	import WorkspacePicker from './WorkspacePicker.svelte';

	const TIERS = ['brain', 'worker', 'validator'] as const;

	interface Props {
		/** Directory picker, injectable for tests. Defaults to the Tauri dialog plugin. */
		pickDirectory?: () => Promise<string | null>;
	}
	let { pickDirectory }: Props = $props();

	const STATE_LABELS: Record<SessionIndicator, string> = {
		none: 'Idle',
		idle: 'Idle',
		running: 'Running',
		awaiting_approval: 'Awaiting approval',
		paused: 'Paused',
		complete: 'Complete',
		failed: 'Failed',
		cancelled: 'Cancelled',
		interrupted: 'Interrupted',
	};

	function stateTone(state: SessionIndicator): string {
		if (state === 'running') return 'info';
		if (state === 'awaiting_approval' || state === 'paused') return 'warning';
		if (state === 'failed') return 'danger';
		if (state === 'complete') return 'success';
		return 'muted';
	}

	let boundaryTip = $derived(
		session.boundary === null
			? null
			: [
					`source: ${session.boundary.source}`,
					`writable: ${session.boundary.writable_paths.join(', ')}`,
					`commands: ${session.boundary.allowed_commands.join(', ')}`,
					`network: ${typeof session.boundary.network === 'string' ? session.boundary.network : session.boundary.network.join(', ')}`,
				].join('\n'),
	);
</script>

<div class="titlebar">
	<WorkspacePicker {pickDirectory} />
	<CuKillSwitch />

	{#if session.sessionId !== null}
		<!-- Tier chips -->
		<div class="tiers" role="group" aria-label="Model tier">
			{#each TIERS as tier (tier)}
				<button
					class="chip"
					class:chip--active={session.tier === tier}
					class:chip--pinned={session.tierOverride === tier}
					type="button"
					aria-pressed={session.tier === tier}
					title={session.modelSlugs[tier]
						? `${session.modelSlugs[tier]}${session.tierOverride === tier ? ' (pinned)' : ''}`
						: tier}
					onclick={() => setTier(tier)}
				>
					{tier}
					{#if session.tierOverride === tier}
						<span class="pin" aria-hidden="true">●</span>
					{/if}
				</button>
			{/each}
			<!-- Plan lock (TD-4603): brain on every completion until cleared.
			     Not a plan document, not accept-to-execute — a tier lock. The
			     meter stays honest: this is the expensive path by design. -->
			<button
				class="chip"
				class:chip--active={session.planLock}
				type="button"
				aria-pressed={session.planLock}
				title={session.planLock
					? 'Plan is on — brain forced every turn until cleared (expensive)'
					: 'Plan — force the brain tier every turn until cleared (expensive)'}
				onclick={() => setPlanMode(!session.planLock)}
			>
				plan
			</button>
		</div>

		<CostMeter />

		<!-- Session state -->
		<span
			class="indicator indicator--{stateTone(session.state)}"
			title={session.reason ?? STATE_LABELS[session.state]}
		>
			<span class="dot" aria-hidden="true"></span>
			{STATE_LABELS[session.state]}
		</span>

		<!-- Boundary (wall) -->
		{#if session.boundary !== null}
			<span class="wall" title={boundaryTip ?? undefined}>
				{formatUsd(session.boundary.spend_usd)} cap · {session.boundary.wall_clock_hours}h · {session.boundary.max_iterations} iter
			</span>
		{/if}
	{/if}
</div>

<style>
	.titlebar {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		min-width: 0;
	}

	.tiers {
		display: inline-flex;
		gap: var(--space-1);
	}

	.chip {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text-secondary);
		background: transparent;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		transition: border-color var(--transition-fast);
	}

	.chip:hover {
		border-color: var(--color-accent);
	}

	.chip--active {
		color: var(--color-accent-text);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.chip--active:hover {
		background: var(--color-accent-hover);
		border-color: var(--color-accent-hover);
	}

	.pin {
		font-size: 8px;
		vertical-align: middle;
	}

	.indicator {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text);
	}

	.indicator .dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-text-muted);
	}

	.indicator--info .dot { background: var(--color-info); }
	.indicator--warning .dot { background: var(--color-warning); }
	.indicator--danger .dot { background: var(--color-danger); }
	.indicator--success .dot { background: var(--color-success); }

	.wall {
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-text-secondary);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-2);
		white-space: nowrap;
	}
</style>
