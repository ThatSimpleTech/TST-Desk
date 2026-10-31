<script lang="ts">
	// Settings → Appearance → Background (TD-5202): pick the background, or
	// turn it off, how strongly the veil behind the panes quiets it, and the
	// Motion and Click ripples switches. Saved per device, like the theme.
	//
	// Each tile shows the real background in miniature — the same component
	// the window uses, drawn into the tile — held still until the pointer or
	// the keyboard focus is on it, then it comes alive. Picking one swaps the
	// window behind this dialog straight away. The tiles are one radio
	// group: arrow keys move the choice, Tab leaves the group.
	//
	// Only keyboard focus counts: Chromium and WebKitGTK also focus a button
	// that is clicked, and a clicked tile would otherwise keep a second copy
	// of the window's background running until focus moved on. That rule is
	// the whole rule for a tile: the window's own background also holds
	// still while another window has the focus, and a tile does not
	// (markPreviews).
	//
	// Strength is a second radio group of two, Subtle and Vivid, in the
	// theme's pill style. It only thins a veil, so it stands down for Off and
	// Classic, which have none, and says so.
	import { BACKGROUNDS, backgroundInfo, type BackgroundId } from '../backgrounds/catalog';
	import { STRENGTHS, type BackgroundStrength } from '../backgrounds/prefs';
	import { markPreviews } from '../backgrounds/preview';
	import {
		background,
		setBackground,
		setBackgroundMotion,
		setBackgroundRipples,
		setBackgroundStrength,
	} from '../backgrounds/store.svelte.js';
	import BackgroundOrbit from '../BackgroundOrbit.svelte';
	import { WINDOW_LAYERS, WINDOW_VEILS } from './backgrounds/registry';

	markPreviews();

	let hovered: BackgroundId | null = $state(null);
	let focused: BackgroundId | null = $state(null);
	let tiles: HTMLButtonElement[] = $state([]);
	let strengths: HTMLButtonElement[] = $state([]);

	const canMove = $derived(background.motion && !background.reducedMotion);
	const isOff = $derived(background.id === 'off');
	const veiled = $derived(WINDOW_VEILS[background.id] !== null);

	const STRENGTH_LABEL: Record<BackgroundStrength, string> = { subtle: 'Subtle', vivid: 'Vivid' };

	function live(id: BackgroundId): boolean {
		return canMove && (hovered === id || focused === id);
	}

	/** Focus the browser would ring: from the keyboard, not a click. An
	 *  engine without :focus-visible counts any focus, as before. */
	function keyboardFocus(el: Element): boolean {
		try {
			return el.matches(':focus-visible');
		} catch {
			return true;
		}
	}

	const STEPS: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };

	/** Where a key moves the choice in a radio group of `count`: the arrows
	 *  step and wrap, Home and End jump. Null for any other key. */
	function moveTo(event: KeyboardEvent, index: number, count: number): number | null {
		let next: number | null = null;
		if (event.key in STEPS) next = (index + STEPS[event.key] + count) % count;
		else if (event.key === 'Home') next = 0;
		else if (event.key === 'End') next = count - 1;
		if (next !== null) event.preventDefault();
		return next;
	}

	function onKey(event: KeyboardEvent, index: number): void {
		const next = moveTo(event, index, BACKGROUNDS.length);
		if (next === null) return;
		setBackground(BACKGROUNDS[next].id);
		tiles[next]?.focus();
	}

	function onStrengthKey(event: KeyboardEvent, index: number): void {
		const next = moveTo(event, index, STRENGTHS.length);
		if (next === null) return;
		setBackgroundStrength(STRENGTHS[next]);
		strengths[next]?.focus();
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
				onfocus={(event) => (focused = keyboardFocus(event.currentTarget) ? option.id : null)}
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
		<p class="title" id="background-strength-title">Strength</p>
		<div class="seg" role="radiogroup" aria-labelledby="background-strength-title">
			{#each STRENGTHS as option, index (option)}
				{@const chosen = background.strength === option}
				<button
					bind:this={strengths[index]}
					class="choice"
					class:choice--active={chosen && veiled}
					type="button"
					role="radio"
					aria-checked={chosen}
					tabindex={chosen ? 0 : -1}
					disabled={!veiled}
					onclick={() => setBackgroundStrength(option)}
					onkeydown={(event) => onStrengthKey(event, index)}>{STRENGTH_LABEL[option]}</button
				>
			{/each}
		</div>
	</div>
	<p class="hint">
		{#if isOff}
			Off draws nothing, so there is nothing to strengthen.
		{:else if !veiled}
			{backgroundInfo(background.id).label} keeps its own look; Strength changes the other backgrounds.
		{:else if background.strength === 'vivid'}
			Vivid lets the background show much more. Small grey text can be harder to read.
		{:else}
			Subtle keeps a wash of the page colour over the background, so all text stays clear.
		{/if}
	</p>

	<div class="row">
		<p class="title" id="background-motion-title">Motion</p>
		<button
			class="choice"
			class:choice--active={background.motion && !isOff}
			type="button"
			role="switch"
			aria-labelledby="background-motion-title"
			aria-checked={background.motion}
			disabled={isOff}
			onclick={() => setBackgroundMotion(!background.motion)}>{background.motion ? 'On' : 'Off'}</button
		>
	</div>
	<div class="row">
		<p class="title" id="background-ripples-title">Click ripples</p>
		<button
			class="choice"
			class:choice--active={background.ripples && !isOff}
			type="button"
			role="switch"
			aria-labelledby="background-ripples-title"
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

	.seg {
		display: inline-flex;
		gap: var(--space-1);
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
