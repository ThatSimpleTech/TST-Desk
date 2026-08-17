<script lang="ts">
	// Title bar (TD-1006): workspace picker, tier chips, live cost meter,
	// session state indicator, and the boundary ("wall") summary. Presentational
	// only — everything shown is reduced from daemon events via session-status;
	// the UI never derives truth it wasn't given (AGENTS §6).
	import {
		session,
		setTier,
		workspaceName,
		type SessionIndicator,
	} from '../session-status.svelte.js';
	import {
		workspaces,
		visibleRecents,
		openRecent,
		hideRecent,
		toggleWorkspaceMenu,
		closeWorkspaceMenu,
		startWorkspaces,
	} from '../workspaces.svelte.js';
	import { formatUsd } from '../cost-format.js';
	import { openUsage } from '../usage.svelte.js';
	import { onMount } from 'svelte';
	import Icon from './Icon.svelte';

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
		if (path !== null) openRecent(path);
	}

	onMount(() => startWorkspaces());

	function onBackdropKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') closeWorkspaceMenu();
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
	<!-- Workspace: the name opens the recents menu (TD-1103 quick switch +
	     per-entry remove); the folder entry in the menu is the picker. -->
	<div class="ws-wrap">
		<button
			class="workspace"
			type="button"
			aria-haspopup="menu"
			aria-expanded={workspaces.menuOpen}
			onclick={toggleWorkspaceMenu}
			title={session.workspacePath ?? 'Open a workspace'}
		>
			<span class="folder"><Icon name="folder" size={12} /></span>
			<span>{displayName ?? 'Open workspace…'}</span>
			<span class="chevron" aria-hidden="true"><Icon name="chevron-down" size={10} /></span>
		</button>

		{#if workspaces.menuOpen}
			<button
				class="ws-backdrop"
				type="button"
				tabindex="-1"
				aria-hidden="true"
				onclick={closeWorkspaceMenu}
				onkeydown={onBackdropKeydown}
			></button>
			<div class="ws-menu" role="menu" aria-label="Recent workspaces">
				{#each visibleRecents() as recent (recent.path)}
					<div class="ws-row" role="none">
						<button
							class="ws-switch"
							type="button"
							role="menuitem"
							title={recent.path}
							onclick={() => openRecent(recent.path)}
						>
							<span class="ws-name">{workspaceName(recent.path)}</span>
							<span class="ws-path">{recent.path}</span>
						</button>
						<button
							class="ws-remove"
							type="button"
							role="menuitem"
							aria-label="Remove {workspaceName(recent.path)} from recents"
							title="Remove from recents"
							onclick={() => hideRecent(recent.path)}
						><Icon name="x" size={10} /></button>
					</div>
				{/each}
				{#if visibleRecents().length > 0}
					<div class="ws-sep" aria-hidden="true"></div>
				{/if}
				<button class="ws-open" type="button" role="menuitem" onclick={pick}>
					<Icon name="folder" size={12} /> Open folder…
				</button>
			</div>
		{/if}
	</div>

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
				<!-- The meter is this session's running spend; the usage pane
				     (TD-1706) is the audit store's history across all of them. -->
				<button class="popover-link" type="button" onclick={openUsage}>
					Usage and cost history →
				</button>
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
		border: 1px solid var(--color-border);
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
		display: inline-flex;
		flex-shrink: 0;
		color: var(--color-text-secondary);
	}

	/* Workspace recents menu (TD-1103) */
	.ws-wrap {
		position: relative;
	}

	.chevron {
		display: inline-flex;
		color: var(--color-text-secondary);
	}

	.ws-backdrop {
		position: fixed;
		inset: 0;
		z-index: 19;
		background: transparent;
		border: 0;
		cursor: default;
	}

	.ws-menu {
		position: absolute;
		top: calc(100% + var(--space-1));
		left: 0;
		z-index: 20;
		min-width: 16rem;
		max-width: 26rem;
		background: var(--color-bg-raised);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-lg);
		padding: var(--space-1);
	}

	.ws-row {
		display: flex;
		align-items: center;
	}

	.ws-switch {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		flex: 1;
		min-width: 0;
		gap: 1px;
		background: transparent;
		border: 0;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		text-align: left;
	}

	.ws-switch:hover {
		background: var(--color-bg-subtle);
	}

	.ws-name {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text);
	}

	.ws-path {
		font-size: 10px;
		font-family: var(--font-mono);
		color: var(--color-text-secondary);
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ws-remove {
		background: transparent;
		border: 0;
		color: var(--color-text-muted);
		font-size: 10px;
		padding: var(--space-1);
		cursor: pointer;
		border-radius: var(--radius-sm);
		flex-shrink: 0;
	}

	.ws-remove:hover {
		color: var(--color-danger);
		background: var(--color-bg-subtle);
	}

	.ws-sep {
		border-top: 1px solid var(--color-border);
		margin: var(--space-1) 0;
	}

	.ws-open {
		display: block;
		width: 100%;
		background: transparent;
		border: 0;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-text);
		cursor: pointer;
		text-align: left;
	}

	.ws-open:hover {
		background: var(--color-bg-subtle);
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
