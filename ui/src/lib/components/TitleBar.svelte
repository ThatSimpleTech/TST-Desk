<script lang="ts">
	// Title bar (TD-1006, TD-1720, TD-4603): workspace picker, Plan lock,
	// tier chips, live model pill, cost meter, session state, and the
	// boundary ("wall"). Presentational only — everything shown is reduced
	// from daemon events via session-status; the UI never derives a host
	// from a slug.
	//
	// The picker and the meter own their own markup and styles in
	// WorkspacePicker/CostMeter; what stays here is the row itself.
	import { liveModelLabel, session, setPlan, setTier, type SessionIndicator } from '../session-status.svelte.js';
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

	const liveLabel = $derived(liveModelLabel(session.tier, session.modelSlugs, session.hosts));
	const liveTip = $derived(
		[
			session.preset ? `preset: ${session.preset}` : null,
			`tier: ${session.tier}`,
			session.modelSlugs[session.tier] ? `model: ${session.modelSlugs[session.tier]}` : null,
			session.hosts[session.tier] ? `host: ${session.hosts[session.tier]}` : null,
		]
			.filter((line): line is string => line !== null)
			.join('\n') || undefined,
	);

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
		<!-- Plan lock (TD-4603) + tier chips -->
		<div class="tiers" role="group" aria-label="Model tier">
			<button
				class="chip"
				class:chip--active={session.planMode}
				type="button"
				aria-pressed={session.planMode}
				title={session.planMode
					? 'Plan mode on — every turn uses brain. Click to clear.'
					: 'Plan mode — lock every turn to brain'}
				onclick={() => setPlan(!session.planMode)}
			>
				Plan
			</button>
			{#each TIERS as tier (tier)}
				<button
					class="chip"
					class:chip--active={session.tier === tier}
					class:chip--pinned={session.tierOverride === tier}
					type="button"
					aria-pressed={session.tier === tier}
					disabled={session.planMode && tier !== 'brain'}
					title={session.planMode && tier !== 'brain'
						? 'Plan mode is on; only brain is allowed'
						: session.modelSlugs[tier] || session.hosts[tier]
							? [
									session.modelSlugs[tier],
									session.hosts[tier],
									session.tierOverride === tier ? '(pinned)' : '',
								]
									.filter(Boolean)
									.join(' ')
							: tier}
					onclick={() => setTier(tier)}
				>
					{tier}
					{#if session.tierOverride === tier}
						<span class="pin" aria-hidden="true">●</span>
					{/if}
				</button>
			{/each}
		</div>

		{#if liveLabel}
			<span class="live-model" title={liveTip} aria-label="Live model">{liveLabel}</span>
		{/if}

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

	.chip:hover:not(:disabled) {
		border-color: var(--color-accent);
	}

	.chip:disabled {
		opacity: 0.45;
		cursor: not-allowed;
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

	.live-model {
		min-width: 0;
		max-width: 22rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--text-xs);
		font-family: var(--font-mono);
		color: var(--color-text-secondary);
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
