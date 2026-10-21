<script lang="ts">
	// Model controls for the composer: Plan (read-only plan mode, TD-5123;
	// `/plan` and the phone use the same chip), the session's model, and (on
	// the grok engine) the mode chips. The brain/worker/validator roles are
	// setup, not a per-message choice, so they have no chip (TD-5172);
	// set_tier stays on the wire for API users.
	import { grok, setGrokMode } from "../../grok.svelte.js";
	import { session, setPlan } from "../../session-status.svelte.js";
	import { settings } from "../../settings.svelte.js";
	import ModelPicker from "./ModelPicker.svelte";

	const grokEngine = $derived((session.engine ?? settings.engine) === "grok");

	const PLAN_HINT =
		"Plan mode: the agent looks around without changing anything, then shows you a plan. Nothing runs until you approve it.";
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
					? "Plan mode is on: the agent only reads, then shows a plan for you to approve. Click to turn it off."
					: PLAN_HINT}
				onclick={() => setPlan(!session.planMode)}
			>
				Plan
			</button>
		{/if}

		{#if settings.presets.length > 0 || session.preset !== "" || !settings.hasModel}
			<ModelPicker />
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
</style>
