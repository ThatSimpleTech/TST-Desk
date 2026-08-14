<script lang="ts">
	// Title bar (TD-1006): workspace picker, tier chips, live cost meter,
	// session state indicator, and the boundary ("wall") summary. Presentational
	// only — everything shown is reduced from daemon events via session-status;
	// the UI never derives truth it wasn't given (AGENTS §6).
	import {
		session,
		openWorkspace,
		setTier,
		workspaceName,
		type SessionIndicator,
	} from '../session-status.svelte.js';

	const TIERS = ['brain', 'worker', 'validator'] as const;

	interface Props {
		/** Directory picker, injectable for tests. Defaults to the Tauri dialog plugin. */
		pickDirectory?: () => Promise<string | null>;
	}
	let { pickDirectory }: Props = $props();

	async function defaultPickDirectory(): Promise<string | null> {
		const { open } = await import('@tauri-apps/plugin-dialog');
		const chosen = await open({ directory: true, multiple: false });
		return typeof chosen === 'string' ? chosen : null;
	}

	async function pick(): Promise<void> {
		const path = await (pickDirectory ?? defaultPickDirectory)();
		if (path !== null) openWorkspace(path);
	}

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

	function formatUsd(n: number): string {
		return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
	}

	let displayName = $derived(
		session.workspacePath !== null ? workspaceName(session.workspacePath) : null,
	);
	let costBreakdown = $derived(
		Object.entries(session.cost.byTier).sort((a, b) => b[1] - a[1]),
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
	<!-- Workspace -->
	<button
		class="workspace"
		type="button"
		onclick={pick}
		title={session.workspacePath ?? 'Open a workspace'}
	>
		<svg class="folder" viewBox="0 0 16 16" aria-hidden="true">
			<path
				d="M1.5 3.5a1 1 0 0 1 1-1h3.19a1 1 0 0 1 .78.37l.94 1.13h6.09a1 1 0 0 1 1 1V12.5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-9z"
				fill="none"
				stroke="currentColor"
				stroke-width="1.2"
			/>
		</svg>
		<span>{displayName ?? 'Open workspace…'}</span>
	</button>

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
		</div>

		<!-- Live cost meter -->
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
			</div>
		</div>

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

	.workspace {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text);
		background: transparent;
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		max-width: 20rem;
		overflow: hidden;
		white-space: nowrap;
	}

	.workspace:hover {
		background: var(--color-bg-subtle);
	}

	.folder {
		width: var(--icon-xs);
		height: var(--icon-xs);
		flex-shrink: 0;
		color: var(--color-text-secondary);
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
		border: var(--border-width) solid var(--color-border);
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
		font-size: var(--text-2xs);
		vertical-align: middle;
	}

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
		border: var(--border-width) solid var(--color-border);
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
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-2);
		white-space: nowrap;
	}
</style>
