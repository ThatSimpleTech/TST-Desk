<script lang="ts">
	// Model controls for the composer: Plan, the brain / worker / validator
	// picker, the session preset, and (on the grok engine) the mode chips.
	// They used to live in the title bar. The menu opens upward because this
	// row sits at the bottom of the window.
	import { grok, setGrokMode } from "../../grok.svelte.js";
	import { session, setPlan, setSessionPreset, setTier } from "../../session-status.svelte.js";
	import { settings } from "../../settings.svelte.js";
	import Icon from "../Icon.svelte";

	const TIERS = ["brain", "worker", "validator"] as const;
	type Tier = (typeof TIERS)[number];

	let menuOpen = $state(false);
	let root: HTMLElement | undefined = $state();

	const grokEngine = $derived((session.engine ?? settings.engine) === "grok");
	const activeSlug = $derived(session.modelSlugs[session.tier] ?? null);
	const presetOptions = $derived(
		session.preset && !settings.presets.includes(session.preset)
			? [session.preset, ...settings.presets]
			: settings.presets,
	);

	function pickTier(tier: Tier): void {
		setTier(tier);
		menuOpen = false;
	}

	function onWindowClick(event: MouseEvent): void {
		if (!menuOpen) return;
		if (root !== undefined && event.target instanceof Node && root.contains(event.target)) return;
		menuOpen = false;
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key !== "Escape" || !menuOpen) return;
		// Peel the menu first. The shell's Esc cancels a live turn, and this
		// press was for the menu.
		event.preventDefault();
		event.stopPropagation();
		menuOpen = false;
	}
</script>

<svelte:window onclick={onWindowClick} />

{#if session.sessionId !== null}
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div class="controls" role="group" aria-label="Model" bind:this={root} onkeydown={onKeydown}>
		{#if grokEngine}
			{#if grok.modes.length > 0 && grok.sessionId === session.sessionId}
				<div class="modes" role="group" aria-label="Grok mode">
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
			<button
				class="chip"
				class:chip--active={session.planMode}
				type="button"
				aria-pressed={session.planMode}
				title={session.planMode
					? "Plan mode on — every turn uses brain. Click to clear."
					: "Plan mode — lock every turn to brain"}
				onclick={() => setPlan(!session.planMode)}
			>
				Plan
			</button>
			<div class="model">
				<button
					class="chip chip--model"
					type="button"
					aria-haspopup="menu"
					aria-expanded={menuOpen}
					title={`${session.tier} tier${activeSlug !== null ? ` · ${activeSlug}` : ""}${session.tierOverride !== null ? " · pinned" : ""}`}
					onclick={() => (menuOpen = !menuOpen)}
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
				{#if menuOpen}
					<div class="menu" role="menu" aria-label="Model tier">
						{#each TIERS as tier (tier)}
							<button
								class="menu-item"
								class:menu-item--active={session.tier === tier}
								role="menuitemradio"
								aria-checked={session.tier === tier}
								type="button"
								disabled={session.planMode && tier !== "brain"}
								onclick={() => pickTier(tier)}
							>
								<span class="menu-tier">
									{tier}
									{#if session.tierOverride === tier}
										<span class="pin" aria-hidden="true">●</span>
									{/if}
								</span>
								<span class="menu-slug">{session.modelSlugs[tier] ?? "—"}</span>
							</button>
						{/each}
						<p class="menu-hint">Choosing a tier pins it for this session.</p>
					</div>
				{/if}
			</div>
		{/if}

		{#if presetOptions.length > 0}
			<select
				class="preset"
				aria-label="Session preset"
				title={session.turnActive
					? "Cannot change preset while a turn is running"
					: "Preset for this session"}
				value={session.preset}
				disabled={session.turnActive}
				onchange={(e) => {
					const name = e.currentTarget.value;
					if (name !== session.preset) setSessionPreset(name);
				}}
			>
				{#each presetOptions as name (name)}
					<option value={name}>{name}</option>
				{/each}
			</select>
		{/if}
	</div>
{/if}

<style>
	.controls {
		position: relative;
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		align-items: center;
		gap: var(--space-2);
		min-width: 0;
		max-width: 100%;
	}

	.modes {
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

	.chip:hover:not(:disabled) {
		border-color: var(--color-ink-muted);
		color: var(--color-ink);
	}

	.chip:disabled {
		opacity: 0.45;
		cursor: not-allowed;
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
		max-width: 16rem;
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

	.preset {
		max-width: 8rem;
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
	}

	.preset:disabled {
		opacity: 0.45;
		cursor: not-allowed;
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
		bottom: calc(100% + var(--space-1));
		right: 0;
		z-index: 5;
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

	.menu-item:disabled {
		opacity: 0.45;
		cursor: not-allowed;
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
</style>
