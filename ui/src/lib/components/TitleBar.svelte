<script lang="ts">
	// Title bar (TD-1006): workspace picker, grok identity, cost meter,
	// session state, and the boundary ("wall"). The model name lives in
	// the composer dropdown.
	// Presentational only — everything shown is reduced from daemon events
	// via session-status; the UI never derives a host from a slug.
	//
	// The picker and the meter own their own markup and styles in
	// WorkspacePicker/CostMeter; what stays here is the row itself.
	import { session, type SessionIndicator } from '../session-status.svelte.js';
	import { settings } from '../settings.svelte.js';
	import { grok } from '../grok.svelte.js';
	import { formatUsd } from '../cost-format.js';
	import CostMeter from './CostMeter.svelte';
	import CuKillSwitch from './CuKillSwitch.svelte';
	import WorkspacePicker from './WorkspacePicker.svelte';

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

	// Spend against the cap, drawn as a bar so the eye reads it without
	// arithmetic. Hours and iterations stay in the tooltip: they are limits
	// nobody watches turn by turn.
	let capUsd = $derived(session.boundary?.spend_usd ?? 0);
	let capRatio = $derived(capUsd > 0 ? Math.min(1, session.cost.session / capUsd) : 0);
	let capTone = $derived(capRatio >= 1 ? 'danger' : capRatio >= 0.8 ? 'warning' : 'ok');

	let boundaryTip = $derived(
		session.boundary === null
			? null
			: [
					`spend cap: ${formatUsd(session.boundary.spend_usd)}`,
					`wall clock: ${session.boundary.wall_clock_hours}h`,
					`iterations: ${session.boundary.max_iterations}`,
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
		{#if (session.engine ?? settings.engine) === 'grok'}
			<span class="chip chip--active" title={settings.grokBinary ?? 'Grok Build CLI'}>grok</span>
			{#if grok.model && grok.sessionId === session.sessionId}
				<span class="chip" title="Grok model">{grok.model}</span>
			{/if}
		{/if}

		<CostMeter />

		<!-- Spend against the boundary's cap (the "wall") -->
		{#if session.boundary !== null}
			<span class="cap cap--{capTone}" title={boundaryTip ?? undefined}>
				<span class="cap-bar" aria-hidden="true">
					<span class="cap-fill" style={`width: ${Math.round(capRatio * 100)}%`}></span>
				</span>
				<span class="cap-text">of {formatUsd(session.boundary.spend_usd)}</span>
			</span>
		{/if}

		<!-- Session state -->
		<span
			class="indicator indicator--{stateTone(session.state)}"
			title={session.reason ?? STATE_LABELS[session.state]}
		>
			<span class="dot" aria-hidden="true"></span>
			{STATE_LABELS[session.state]}
		</span>
	{/if}
</div>

<style>
	.titlebar {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		min-width: 0;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
	}

	.chip--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.indicator {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		white-space: nowrap;
	}

	.indicator .dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-ink-muted);
	}

	.indicator--info .dot { background: var(--color-accent); }
	.indicator--warning .dot { background: var(--color-warn); }
	.indicator--danger .dot { background: var(--color-err); }
	.indicator--success .dot { background: var(--color-ok); }

	.cap {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		white-space: nowrap;
	}

	.cap-bar {
		display: block;
		width: 64px;
		height: 4px;
		border-radius: var(--radius-full);
		background: var(--color-hairline);
		overflow: hidden;
	}

	.cap-fill {
		display: block;
		height: 100%;
		border-radius: var(--radius-full);
		background: var(--color-accent);
		transition: width var(--transition-base);
	}

	.cap--warning .cap-fill { background: var(--color-warn); }
	.cap--danger .cap-fill { background: var(--color-err); }
</style>
