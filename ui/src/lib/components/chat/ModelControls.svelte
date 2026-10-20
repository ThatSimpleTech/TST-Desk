<script lang="ts">
	// Model controls for the composer: Plan (read-only plan mode, TD-5123;
	// `/plan` and the phone use the same chip), a tier dropdown, the session
	// preset, and (on the grok engine) the mode chips. The dropdown names
	// each role with the model that role is actually running: brain (kimi).
	import { tierMenuLabel } from "../../composer-layout";
	import { grok, setGrokMode } from "../../grok.svelte.js";
	import { session, setPlan, setSessionPreset, setTier } from "../../session-status.svelte.js";
	import { settings } from "../../settings.svelte.js";

	const TIERS = ["brain", "worker", "validator"] as const;
	type Tier = (typeof TIERS)[number];

	const grokEngine = $derived((session.engine ?? settings.engine) === "grok");
	const presetOptions = $derived(
		session.preset && !settings.presets.includes(session.preset)
			? [session.preset, ...settings.presets]
			: settings.presets,
	);

	function isTier(value: string): value is Tier {
		return value === "brain" || value === "worker" || value === "validator";
	}

	function onTierChange(event: Event): void {
		const next = (event.currentTarget as HTMLSelectElement).value;
		if (!isTier(next) || next === session.tier) return;
		setTier(next);
	}
</script>

{#if session.sessionId !== null}
	<div class="controls" role="group" aria-label="Model">
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
					? "Plan mode on: read-only, every turn uses brain. The agent ends it with a plan for you to approve. Click to clear."
					: "Plan mode: the agent researches read-only on brain, then puts up a plan for you to approve"}
				onclick={() => setPlan(!session.planMode)}
			>
				Plan
			</button>
			<span class="menu">
				<select
					class="tier"
					aria-label="Model tier"
					title={session.tierOverride !== null
						? "Pinned for this session. Choosing a tier pins it."
						: "Choosing a tier pins it for this session."}
					value={session.tier}
					onchange={onTierChange}
				>
					{#each TIERS as tier (tier)}
						<option value={tier} disabled={session.planMode && tier !== "brain"}>
							{tierMenuLabel(tier, session.modelSlugs[tier], session.fallbacks[tier])}
						</option>
					{/each}
				</select>
			</span>
		{/if}

		{#if presetOptions.length > 0}
			<span class="menu">
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
			</span>
		{/if}
	</div>
{/if}

<style>
	.controls {
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-start;
		align-items: center;
		gap: var(--space-1);
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
		border: none;
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		transition:
			background var(--transition-fast),
			color var(--transition-fast);
	}

	.chip:hover:not(:disabled) {
		background: var(--color-sunken);
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

	.menu {
		position: relative;
		display: inline-flex;
		align-items: center;
		min-width: 0;
		max-width: 100%;
	}

	.menu::after {
		content: "";
		position: absolute;
		right: var(--space-2);
		width: 5px;
		height: 5px;
		border-right: 1.5px solid var(--color-ink-muted);
		border-bottom: 1.5px solid var(--color-ink-muted);
		transform: translateY(-1px) rotate(45deg);
		pointer-events: none;
	}

	.tier,
	.preset {
		appearance: none;
		max-width: 8rem;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
		background: transparent;
		border: none;
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-5) var(--space-1) var(--space-2);
		cursor: pointer;
	}

	.tier:hover,
	.preset:hover {
		background: var(--color-sunken);
		color: var(--color-ink);
	}

	.tier {
		max-width: 18rem;
	}

	.preset:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
</style>
