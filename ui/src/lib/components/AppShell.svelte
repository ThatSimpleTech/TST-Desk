<script lang="ts">
	// Application shell: two-pane workspace layout using only design tokens.
	// Left = chat pane, right = activity pane. Both are placeholder chrome in
	// v0.1; their real content arrives with TD-1003/TD-1004.
	import SplitPane from './SplitPane.svelte';
</script>

<header class="shell-header">
	<span class="shell-title">TST Desk</span>
</header>

<div class="shell-body">
	<SplitPane>
		{#snippet left()}
			<section class="pane-chat" aria-label="Chat pane"></section>
		{/snippet}
		{#snippet right()}
			<section class="pane-activity" aria-label="Activity pane"></section>
		{/snippet}
	</SplitPane>
</div>

<style>
	.shell-header {
		display: flex;
		align-items: center;
		height: var(--space-12);
		padding: 0 var(--space-6);
		border-bottom: var(--border-width) solid var(--color-border);
		background: var(--color-bg-raised);
		flex-shrink: 0;
	}

	.shell-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-text);
	}

	.shell-body {
		flex: 1;
		min-height: 0;
		display: flex;
	}

	.pane-chat,
	.pane-activity {
		height: 100%;
		background: var(--color-bg);
		color: var(--color-text);
	}

	/* Subtle tonal separation so the two-pane split reads at a glance. */
	.pane-activity {
		background: var(--color-bg-subtle);
	}
</style>
