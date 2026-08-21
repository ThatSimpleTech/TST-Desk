<script lang="ts">
	// Settings toggles for computer-use indicators (TD-3402).
	import { setCuIndicators, settings } from '../settings.svelte.js';
</script>

<div class="group">
	<div class="row">
		<p class="title">Computer-use glow</p>
		<button
			class="choice"
			class:choice--active={settings.cuGlow}
			type="button"
			role="switch"
			aria-checked={settings.cuGlow}
			onclick={() => setCuIndicators({ glow: !settings.cuGlow })}
			>{settings.cuGlow ? 'On' : 'Off'}</button
		>
	</div>
	<div class="row">
		<p class="title">Agent cursor</p>
		<button
			class="choice"
			class:choice--active={settings.cuAgentCursor}
			type="button"
			role="switch"
			aria-checked={settings.cuAgentCursor}
			onclick={() => setCuIndicators({ agentCursor: !settings.cuAgentCursor })}
			>{settings.cuAgentCursor ? 'On' : 'Off'}</button
		>
	</div>
	<div class="row">
		<p class="title">Show indicators on the real display</p>
		<button
			class="choice"
			class:choice--active={settings.cuShowOnRealDisplay}
			type="button"
			role="switch"
			aria-checked={settings.cuShowOnRealDisplay}
			onclick={() => setCuIndicators({ showOnRealDisplay: !settings.cuShowOnRealDisplay })}
			>{settings.cuShowOnRealDisplay ? 'On' : 'Off'}</button
		>
	</div>
	<p class="hint">
		Glow and the agent cursor sit on the Screen pane. The real-display toggle is off by
		default; when on, a host overlay is hidden during every screenshot. This is not a
		second hardware pointer.
	</p>
</div>

<style>
	.group {
		margin-top: var(--space-4);
		padding-top: var(--space-4);
		border-top: 1px solid var(--color-hairline);
	}

	.row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		margin-top: var(--space-3);
	}

	.row:first-child {
		margin-top: 0;
	}

	.title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: 0;
	}

	.choice {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
		text-transform: capitalize;
	}

	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}
</style>
