<script lang="ts">
	// Screen pane (TD-1710, TD-3401): watch the session browser or desktop.
	//
	// Frames arrive as `screen_frame` with a session-dir path. Preview
	// is a data-URL the host read under the same wall as artifacts.
	// TD-3402: glow + agent cursor overlays hug the frame, not the pane.
	// TD-3403: DesignLayer + a visible Design control (⌘⇧D also works).
	import { session } from '../session-status.svelte.js';
	import { screen } from '../screen.svelte.js';
	import { design, toggleDesign } from '../design.svelte.js';
	import { SCREEN_EMPTY_COPY, screenTabVisible } from '../screen';
	import GlowLayer from './GlowLayer.svelte';
	import AgentCursor from './AgentCursor.svelte';
	import DesignLayer from './DesignLayer.svelte';

	let frameEl: HTMLImageElement | null = $state(null);

	let visible = $derived(
		screenTabVisible({
			boundSessionId: screen.boundSessionId,
			sessionId: session.sessionId,
			hasFrame: screen.hasFrame,
			hasCuTool: screen.hasCuTool,
		}),
	);
	let empty = $derived(screen.preview === null && design.frozenPreview === null);
	let frameSrc = $derived(
		design.enabled && design.frozenPreview !== null ? design.frozenPreview : screen.preview,
	);
	let canDesign = $derived(!empty && frameSrc !== null && !design.actuating);

	function onDesign(): void {
		toggleDesign();
	}
</script>

<div class="screen-pane">
	{#if visible && !empty}
		<div class="toolbar">
			{#if design.enabled}
				<p class="mode" aria-live="polite">Design — click to select · shift-click adds · shift-drag a region</p>
			{:else}
				<p class="mode mode--watch">Watch surface</p>
			{/if}
			<button
				class="design"
				class:design--on={design.enabled}
				type="button"
				aria-pressed={design.enabled}
				disabled={!canDesign && !design.enabled}
				title="Design mode (⌘⇧D)"
				onclick={onDesign}
			>
				Design
			</button>
		</div>
	{/if}
	<div class="stage">
		{#if !visible || empty || frameSrc === null}
			<p class="empty">{SCREEN_EMPTY_COPY}</p>
			{#if screen.error !== null}
				<p class="error">{screen.error}</p>
			{/if}
		{:else}
			<div class="frame-wrap">
				<img bind:this={frameEl} class="frame" src={frameSrc} alt="Computer-use screen" />
				<div class="overlay">
					<DesignLayer image={frameEl} />
					<GlowLayer />
					<AgentCursor />
				</div>
			</div>
		{/if}
	</div>
</div>

<style>
	.screen-pane {
		height: 100%;
		overflow: hidden;
		display: flex;
		flex-direction: column;
		background: var(--color-bg);
	}

	.toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		flex-shrink: 0;
		padding: var(--space-2) var(--space-3);
		border-bottom: var(--border-width) solid var(--color-hairline);
	}

	.mode {
		margin: 0;
		color: var(--color-accent);
		font-size: var(--text-xs);
		min-width: 0;
	}

	.mode--watch {
		color: var(--color-text-muted);
	}

	.design {
		flex-shrink: 0;
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
	}

	.design:hover:not(:disabled) {
		border-color: var(--color-accent);
		color: var(--color-accent);
	}

	.design--on {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.design:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.stage {
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.empty {
		padding: var(--space-6);
		color: var(--color-text-muted);
		font-size: var(--text-sm);
		line-height: var(--leading-relaxed);
		text-align: center;
		max-width: 28rem;
	}

	.error {
		padding: 0 var(--space-6);
		color: var(--color-danger);
		font-size: var(--text-sm);
	}

	.frame-wrap {
		display: grid;
		max-width: 100%;
		max-height: 100%;
		min-height: 0;
	}

	.frame,
	.overlay {
		grid-area: 1 / 1;
		max-width: 100%;
		max-height: 100%;
	}

	.frame {
		object-fit: contain;
		display: block;
	}

	.overlay {
		position: relative;
		pointer-events: none;
	}
</style>
