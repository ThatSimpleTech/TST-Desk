<script lang="ts">
	// Settings → Appearance → Background (TD-5202): pick the background, or
	// turn it off, plus the Motion and Click ripples switches. Saved per
	// device, like the theme.
	//
	// Each tile shows the real background in miniature — the same component
	// the window uses, drawn into the tile — held still until the pointer or
	// the keyboard focus is on it, then it comes alive. Picking one swaps the
	// window behind this dialog straight away. The tiles are one radio
	// group: arrow keys move the choice, Tab leaves the group.
	import { BACKGROUNDS, backgroundInfo, type BackgroundId } from '../backgrounds/catalog';
	import {
		background,
		setBackground,
		setBackgroundMotion,
		setBackgroundRipples,
	} from '../backgrounds/store.svelte.js';
	import BackgroundOrbit from '../BackgroundOrbit.svelte';
	import { WINDOW_LAYERS } from './backgrounds/registry';

	let hovered: BackgroundId | null = $state(null);
	let focused: BackgroundId | null = $state(null);
	const tiles: HTMLButtonElement[] = [];

	const canMove = $derived(background.motion && !background.reducedMotion);
	const isOff = $derived(background.id === 'off');

	function live(id: BackgroundId): boolean {
		return canMove && (hovered === id || focused === id);
	}

	const STEPS: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };

	function onKey(event: KeyboardEvent, index: number): void {
		let next: number | null = null;
		if (event.key in STEPS) next = (index + STEPS[event.key] + BACKGROUNDS.length) % BACKGROUNDS.length;
		else if (event.key === 'Home') next = 0;
		else if (event.key === 'End') next = BACKGROUNDS.length - 1;
		if (next === null) return;
		event.preventDefault();
		setBackground(BACKGROUNDS[next].id);
		tiles[next]?.focus();
	}
</script>

<div class="group">
	<p class="title" id="background-title">Background</p>
	<div class="tiles" role="radiogroup" aria-labelledby="background-title">
		{#each BACKGROUNDS as option, index (option.id)}
			{@const Layer = WINDOW_LAYERS[option.id]}
			{@const chosen = background.id === option.id}
			<button
				bind:this={tiles[index]}
				class="tile"
				class:tile--active={chosen}
				type="button"
				role="radio"
				aria-checked={chosen}
				aria-label={option.label}
				tabindex={chosen ? 0 : -1}
				onclick={() => setBackground(option.id)}
				onkeydown={(event) => onKey(event, index)}
				onpointerenter={() => (hovered = option.id)}
				onpointerleave={() => (hovered = null)}
				onfocus={() => (focused = option.id)}
				onblur={() => (focused = null)}
			>
				<span class="preview">
					{#if option.id === 'classic'}
						<span class="preview-band" aria-hidden="true"></span>
						<BackgroundOrbit animate={live(option.id)} fill="parent" />
					{:else if Layer}
						<Layer animate={live(option.id)} fill="parent" />
					{:else}
						<span class="off-mark" aria-hidden="true"></span>
					{/if}
				</span>
				<span class="name">{option.label}</span>
			</button>
		{/each}
	</div>
	<p class="hint">{backgroundInfo(background.id).blurb}</p>

	<div class="row">
		<p class="title">Motion</p>
		<button
			class="choice"
			class:choice--active={background.motion && !isOff}
			type="button"
			role="switch"
			aria-checked={background.motion}
			disabled={isOff}
			onclick={() => setBackgroundMotion(!background.motion)}>{background.motion ? 'On' : 'Off'}</button
		>
	</div>
	<div class="row">
		<p class="title">Click ripples</p>
		<button
			class="choice"
			class:choice--active={background.ripples && !isOff}
			type="button"
			role="switch"
			aria-checked={background.ripples}
			disabled={isOff}
			onclick={() => setBackgroundRipples(!background.ripples)}>{background.ripples ? 'On' : 'Off'}</button
		>
	</div>
	<p class="hint">
		{#if background.reducedMotion}
			Your system asks for less motion, so the background holds still and clicks don't ripple.
		{:else if isOff}
			Off has nothing to move and no ripples.
		{:else}
			Motion off holds a still frame of the same background. Ripples light where you click in
			the chat.
		{/if}
	</p>
</div>

<style>
	.group {
		margin-top: var(--space-4);
		padding-top: var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
	}

	.title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: 0;
	}

	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(7rem, 1fr));
		gap: var(--space-2);
		margin-top: var(--space-3);
	}

	.tile {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		padding: var(--space-1);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		color: var(--color-ink-secondary);
		text-align: left;
		cursor: pointer;
		transition:
			border-color var(--transition-fast),
			box-shadow var(--transition-fast),
			color var(--transition-fast);
	}

	.tile:hover {
		border-color: var(--color-ink-muted);
		color: var(--color-ink);
	}

	.tile--active,
	.tile--active:hover {
		border-color: var(--color-accent);
		box-shadow: 0 0 0 var(--border-width) var(--color-accent);
		color: var(--color-ink);
	}

	/* A box of the page ground with the background drawn inside it; the
	   stacking context keeps the layers in the tile. */
	.preview {
		position: relative;
		display: block;
		aspect-ratio: 16 / 10;
		overflow: hidden;
		isolation: isolate;
		border-radius: var(--radius-sm);
		background: var(--color-ground);
	}

	/* Classic's light under the header, in miniature. */
	.preview-band {
		position: absolute;
		inset: 0;
		background: radial-gradient(
			ellipse 70% 45% at 30% 0%,
			color-mix(in srgb, var(--color-accent) 30%, transparent),
			transparent 100%
		);
	}

	/* Off: the plain ground, marked with a quiet slashed circle. */
	.off-mark {
		position: absolute;
		top: 50%;
		left: 50%;
		width: var(--space-6);
		height: var(--space-6);
		transform: translate(-50%, -50%) rotate(-45deg);
		border: calc(var(--border-width) * 1.5) solid var(--color-ink-muted);
		border-radius: var(--radius-full);
		opacity: 0.55;
	}

	.off-mark::after {
		content: '';
		position: absolute;
		top: 50%;
		left: 0;
		right: 0;
		border-top: calc(var(--border-width) * 1.5) solid var(--color-ink-muted);
	}

	.name {
		padding: 0 var(--space-1) var(--space-1);
		font-size: var(--text-xs);
	}

	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}

	.row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		margin-top: var(--space-3);
	}

	.choice {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.choice:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
