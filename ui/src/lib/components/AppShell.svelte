<script lang="ts">
	// Application shell: two-pane workspace layout using only design tokens.
	// Left = chat pane, right = activity pane. The connection banner (TD-1003)
	// sits in the shell header so daemon/socket state is visible at all times.
	// The activity pane hosts the activity timeline (TD-1005), fed live from
	// the daemon event stream, and the resolved-stack panel (TD-1201) behind
	// an Activity | Stack tab strip. Failure notices (TD-1008) render as
	// banners under the header (blocking) or toasts bottom-right (transient).
	import { onMount } from 'svelte';
	import SplitPane from './SplitPane.svelte';
	import ConnectionBanner from '../ConnectionBanner.svelte';
	import ActivityTimeline from './ActivityTimeline.svelte';
	import StackPanel from './StackPanel.svelte';
	import { onEvent } from '../connection-status.svelte.js';
	import { push } from '../timeline-store.svelte.js';
	import ChatPane from './chat/ChatPane.svelte';
	import TitleBar from './TitleBar.svelte';
	import NotificationBanner from '../NotificationBanner.svelte';
	import ToastStack from '../ToastStack.svelte';

	// Feed every daemon event into the timeline for the lifetime of the shell.
	onMount(() => onEvent(push));

	let rightTab = $state<'activity' | 'stack'>('activity');
</script>

<header class="shell-header">
	<span class="shell-title">TST Desk</span>
	<TitleBar />
	<span class="shell-spacer"></span>
	<ConnectionBanner />
</header>

<NotificationBanner />

<div class="shell-body">
	<SplitPane>
		{#snippet left()}
			<section class="pane-chat" aria-label="Chat pane"><ChatPane /></section>
		{/snippet}
		{#snippet right()}
			<section class="pane-activity" aria-label="Activity and stack pane">
				<div class="pane-tabs" role="tablist" aria-label="Right pane views">
					<button
						role="tab"
						aria-selected={rightTab === 'activity'}
						class="tab"
						class:tab-active={rightTab === 'activity'}
						onclick={() => (rightTab = 'activity')}
					>
						Activity
					</button>
					<button
						role="tab"
						aria-selected={rightTab === 'stack'}
						class="tab"
						class:tab-active={rightTab === 'stack'}
						onclick={() => (rightTab = 'stack')}
					>
						Stack
					</button>
				</div>
				{#if rightTab === 'activity'}
					<ActivityTimeline />
				{:else}
					<StackPanel />
				{/if}
			</section>
		{/snippet}
	</SplitPane>
</div>

<ToastStack />

<style>
	.shell-header {
		display: flex;
		align-items: center;
		gap: var(--space-4);
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

	/* Push the connection banner to the right edge of the shell header. */
	.shell-spacer {
		flex: 1;
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
		display: flex;
		flex-direction: column;
	}

	.pane-tabs {
		display: flex;
		gap: var(--space-1);
		padding: var(--space-2) var(--space-4) 0;
		border-bottom: var(--border-width) solid var(--color-border);
		flex-shrink: 0;
	}

	.tab {
		padding: var(--space-1) var(--space-3);
		border: none;
		border-bottom: 2px solid transparent;
		background: none;
		color: var(--color-text-muted);
		font-size: var(--text-sm);
		cursor: pointer;
	}

	.tab:hover {
		color: var(--color-text);
	}

	.tab-active {
		color: var(--color-text);
		font-weight: var(--weight-semibold);
		border-bottom-color: var(--color-info);
	}

	.pane-activity > :global(.timeline),
	.pane-activity > :global(.stack-panel) {
		flex: 1;
		min-height: 0;
	}
</style>
