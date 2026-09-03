<script lang="ts">
	// Title bar (TD-1006): workspace picker, the model chip with its tier
	// menu, the live cost meter and its spend-against-cap bar, and the session
	// state indicator. Presentational only — everything shown is reduced from
	// daemon events via session-status; the UI never derives truth it wasn't
	// given (AGENTS §6).
	//
	// The picker and the meter own their own markup and styles in
	// WorkspacePicker/CostMeter; what stays here is the row itself.
	import { session, setTier, type SessionIndicator } from '../session-status.svelte.js';
	import { settings } from '../settings.svelte.js';
	import { grok, setGrokMode } from '../grok.svelte.js';
	import { formatUsd } from '../cost-format.js';
	import Icon from './Icon.svelte';
	import CostMeter from './CostMeter.svelte';
	import CuKillSwitch from './CuKillSwitch.svelte';
	import WorkspacePicker from './WorkspacePicker.svelte';

	const TIERS = ['brain', 'worker', 'validator'] as const;
	type Tier = (typeof TIERS)[number];

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

	// One chip names the model in use; the three tiers wait in its menu.
	// Three permanent chips asked "which one?" of a choice most turns never
	// make. Picking a tier pins it, exactly as the chips did.
	let tierMenuOpen = $state(false);
	let tierRoot: HTMLElement | undefined = $state();
	let activeSlug = $derived(session.modelSlugs[session.tier] ?? null);

	function pickTier(tier: Tier): void {
		setTier(tier);
		tierMenuOpen = false;
	}

	function onWindowClick(event: MouseEvent): void {
		if (!tierMenuOpen) return;
		if (tierRoot !== undefined && event.target instanceof Node && tierRoot.contains(event.target)) {
			return;
		}
		tierMenuOpen = false;
	}

	function onTierKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || !tierMenuOpen) return;
		// The menu is the layer to peel; the shell's global Esc (which would
		// cancel a live turn) must not see this press.
		event.preventDefault();
		event.stopPropagation();
		tierMenuOpen = false;
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

<svelte:window onclick={onWindowClick} />

<div class="titlebar">
	<WorkspacePicker {pickDirectory} />
	<CuKillSwitch />

	{#if session.sessionId !== null}
		{#if settings.engine === 'grok'}
			<span class="chip chip--active" title={settings.grokBinary ?? 'Grok Build CLI'}>grok</span>
			{#if grok.modes.length > 0 && session.sessionId !== null}
				<div class="tiers" role="group" aria-label="Grok mode">
					{#each grok.modes as mode (mode)}
						<button
							class="chip"
							class:chip--active={grok.mode === mode}
							type="button"
							onclick={() => session.sessionId && setGrokMode(session.sessionId, mode)}
						>
							{mode}
						</button>
					{/each}
				</div>
			{/if}
		{:else}
			<!-- Model chip + tier menu -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="model" bind:this={tierRoot} onkeydown={onTierKeydown}>
				<button
					class="chip chip--model"
					type="button"
					aria-haspopup="menu"
					aria-expanded={tierMenuOpen}
					title={`${session.tier} tier${activeSlug !== null ? ` · ${activeSlug}` : ''}${session.tierOverride !== null ? ' · pinned' : ''}`}
					onclick={() => (tierMenuOpen = !tierMenuOpen)}
				>
					<span class="chip-tier">{session.tier}</span>
					{#if activeSlug !== null}
						<span class="chip-slug">{activeSlug}</span>
					{/if}
					{#if session.tierOverride !== null}
						<span class="pin" aria-hidden="true">●</span>
					{/if}
					<span class="chip-caret" aria-hidden="true"><Icon name="chevron-down" size={12} /></span>
				</button>
				{#if tierMenuOpen}
					<div class="menu" role="menu" aria-label="Model tier">
						{#each TIERS as tier (tier)}
							<button
								class="menu-item"
								class:menu-item--active={session.tier === tier}
								role="menuitemradio"
								aria-checked={session.tier === tier}
								type="button"
								onclick={() => pickTier(tier)}
							>
								<span class="menu-tier">
									{tier}
									{#if session.tierOverride === tier}
										<span class="pin" aria-hidden="true">●</span>
									{/if}
								</span>
								<span class="menu-slug">{session.modelSlugs[tier] ?? '—'}</span>
							</button>
						{/each}
						<p class="menu-hint">Choosing a tier pins it for this session.</p>
					</div>
				{/if}
			</div>
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

	.tiers {
		display: inline-flex;
		gap: var(--space-1);
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
		cursor: pointer;
		transition:
			border-color var(--transition-fast),
			color var(--transition-fast);
	}

	.chip:hover {
		border-color: var(--color-ink-muted);
		color: var(--color-ink);
	}

	.chip--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.chip--active:hover {
		background: var(--color-accent-hover);
		border-color: var(--color-accent-hover);
		color: var(--color-on-accent);
	}

	.chip--model {
		max-width: 20rem;
		padding-right: var(--space-1);
	}

	.chip-tier {
		color: var(--color-ink);
	}

	.chip-slug {
		font-family: var(--font-mono);
		font-weight: var(--weight-normal);
		color: var(--color-ink-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.chip-caret {
		display: inline-flex;
		color: var(--color-ink-muted);
	}

	.pin {
		font-size: 8px;
		vertical-align: middle;
		color: var(--color-accent);
	}

	.model {
		position: relative;
		min-width: 0;
	}

	.menu {
		position: absolute;
		top: calc(100% + var(--space-1));
		left: 0;
		z-index: 10;
		min-width: 16rem;
		display: flex;
		flex-direction: column;
		padding: var(--space-1);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-lg);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.menu-item {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-3);
		width: 100%;
		padding: var(--space-1) var(--space-2);
		border: 0;
		background: transparent;
		border-radius: var(--radius-sm);
		text-align: left;
		font-size: var(--text-xs);
		color: var(--color-ink);
		cursor: pointer;
	}

	.menu-item:hover {
		background: var(--color-sunken);
	}

	.menu-item--active .menu-tier {
		color: var(--color-accent);
		font-weight: var(--weight-semibold);
	}

	.menu-slug {
		font-family: var(--font-mono);
		color: var(--color-ink-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.menu-hint {
		margin: var(--space-1) var(--space-2) var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
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
