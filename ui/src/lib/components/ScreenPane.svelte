<script lang="ts">
	// Screen pane (TD-1710, TD-3401): watch the session browser or desktop.
	//
	// Frames arrive as `screen_frame` with a session-dir path. Preview
	// is a data-URL the host read under the same wall as artifacts.
	import { session } from '../session-status.svelte.js';
	import { screen } from '../screen.svelte.js';
	import { SCREEN_EMPTY_COPY, screenTabVisible } from '../screen';
	import GlowLayer from './GlowLayer.svelte';
	import AgentCursor from './AgentCursor.svelte';

	let visible = $derived(
		screenTabVisible({
			boundSessionId: screen.boundSessionId,
			sessionId: session.sessionId,
			hasFrame: screen.hasFrame,
			hasCuTool: screen.hasCuTool,
		}),
	);
	let empty = $derived(screen.preview === null);
</script>

<div class="screen-pane">
	<div class="stage">
		{#if !visible || empty}
			<p class="empty">{SCREEN_EMPTY_COPY}</p>
			{#if screen.error !== null}
				<p class="error">{screen.error}</p>
			{/if}
		{:else}
			<img class="frame" src={screen.preview} alt="Computer-use screen" />
		{/if}
		<GlowLayer />
		<AgentCursor />
	</div>
</div>

<style>
	.screen-pane {
		height: 100%;
		overflow: hidden;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		background: var(--color-bg);
	}

	.stage {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		max-width: 100%;
		max-height: 100%;
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

	.frame {
		max-width: 100%;
		max-height: 100%;
		object-fit: contain;
	}
</style>
