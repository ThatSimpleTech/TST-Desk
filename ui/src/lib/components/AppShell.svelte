<script lang="ts">
	// Application shell: two-pane workspace layout using only design tokens.
	// Left = chat pane, right = activity pane. The title bar (TD-1006) and
	// connection banner (TD-1003) sit in the shell header so daemon/socket
	// state is visible at all times. The activity pane hosts the activity
	// timeline (TD-1005), fed live from the daemon event stream. Failure
	// notices (TD-1008) render as banners under the header (blocking) or
	// toasts bottom-right (transient); the footer hosts pending approval
	// cards (TD-1007).
	import { onMount } from 'svelte';
	import SplitPane from './SplitPane.svelte';
	import ConnectionBanner from '../ConnectionBanner.svelte';
	import ActivityTimeline from './ActivityTimeline.svelte';
	import ApprovalBar from './ApprovalBar.svelte';
	import { onEvent } from '../connection-status.svelte.js';
	import { push } from '../timeline-store.svelte.js';
	import ChatPane from './chat/ChatPane.svelte';
	import TitleBar from './TitleBar.svelte';
	import NotificationBanner from '../NotificationBanner.svelte';
	import ToastStack from '../ToastStack.svelte';
	import WizardPane from './WizardPane.svelte';
	import { start as startOnboarding, reopen as reopenWizard } from '../onboarding.svelte.js';

	// Feed every daemon event into the timeline for the lifetime of the shell,
	// and start first-run detection (TD-1101) — the wizard probes setup state
	// after each handshake and opens when no API key is stored.
	onMount(() => {
		const offTimeline = onEvent(push);
		const offWizard = startOnboarding();
		return () => {
			offTimeline();
			offWizard();
		};
	});
</script>

<header class="shell-header">
	<span class="shell-title">TST Desk</span>
	<TitleBar />
	<span class="shell-spacer"></span>
	<!-- Revisit first-run setup (TD-1101) at any time. -->
	<button
		class="shell-gear"
		type="button"
		title="Setup wizard"
		aria-label="Open setup wizard"
		onclick={reopenWizard}>⚙</button
	>
	<ConnectionBanner />
</header>

<NotificationBanner />

<div class="shell-body">
	<SplitPane>
		{#snippet left()}
			<section class="pane-chat" aria-label="Chat pane"><ChatPane /></section>
		{/snippet}
		{#snippet right()}
			<section class="pane-activity" aria-label="Activity pane">
				<ActivityTimeline />
			</section>
		{/snippet}
	</SplitPane>
</div>

<ApprovalBar />
<ToastStack />
<WizardPane />

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

	/* Revisit affordance for the setup wizard (TD-1101). */
	.shell-gear {
		border: none;
		background: transparent;
		font-size: var(--text-base);
		color: var(--color-text-secondary);
		cursor: pointer;
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-md);
		line-height: 1;
	}

	.shell-gear:hover {
		background: var(--color-bg-subtle);
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
