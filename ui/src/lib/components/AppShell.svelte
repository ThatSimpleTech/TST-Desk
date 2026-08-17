<script lang="ts">
	// Application shell: two-pane workspace layout using only design tokens.
	// Left = chat pane, right = activity pane. The title bar (TD-1006) and
	// connection banner (TD-1003) sit in the shell header so daemon/socket
	// state is visible at all times. The activity pane hosts the activity
	// timeline (TD-1005), fed live from the daemon event stream, and the
	// resolved-stack panel (TD-1201) behind an Activity | Stack tab strip.
	// Failure notices (TD-1008) render as banners under the header (blocking)
	// or toasts bottom-right (transient); the footer hosts pending approval
	// cards (TD-1007).
	import { onMount } from 'svelte';
	import SplitPane from './SplitPane.svelte';
	import SessionRail from './SessionRail.svelte';
	import ConnectionBanner from '../ConnectionBanner.svelte';
	import ActivityTimeline from './ActivityTimeline.svelte';
	import StackPanel from './StackPanel.svelte';
	import ApprovalBar from './ApprovalBar.svelte';
	import { onEvent } from '../connection-status.svelte.js';
	import { push } from '../timeline-store.svelte.js';
	import ChatPane from './chat/ChatPane.svelte';
	import TitleBar from './TitleBar.svelte';
	import NotificationBanner from '../NotificationBanner.svelte';
	import ToastStack from '../ToastStack.svelte';
	import WizardPane from './WizardPane.svelte';
	import DoctorPane from './DoctorPane.svelte';
	import DecisionsPane from './DecisionsPane.svelte';
	import SettingsPane from './SettingsPane.svelte';
	import CommandPalette from './CommandPalette.svelte';
	import Icon from './Icon.svelte';
	import { start as startOnboarding, onboarding } from '../onboarding.svelte.js';
	import { startSettings, openSettings, settings } from '../settings.svelte.js';
	import { startDoctor, runDoctor, doctor } from '../doctor.svelte.js';
	import { startDecisions, openDecisions, decisions } from '../decisions.svelte.js';
	import { palette, openPalette, closePalette } from '../palette-store.svelte.js';
	import { rightPane, showRightPane } from '../right-pane.svelte.js';
	import { resolveShortcut } from '../shortcuts';
	import { chat, cancelTurn } from '../chat-store.svelte.js';
	import { showCancel } from '../chat-store';
	import { workspaces, closeWorkspaceMenu } from '../workspaces.svelte.js';

	// Global shortcuts (TD-1609): Esc peels layers (menu → palette → modal →
	// turn), ⌘, opens settings (TD-1703), ⌘K the command palette (TD-1707).
	// The mapping itself is pure — see shortcuts.ts. One listener, one layer
	// order: no pane installs a keydown handler of its own.
	function onGlobalKeydown(event: KeyboardEvent): void {
		const action = resolveShortcut(event, {
			workspaceMenuOpen: workspaces.menuOpen,
			paletteOpen: palette.open,
			modalOpen:
				onboarding.open || doctor.open || decisions.open || settings.open || palette.open,
			turnLive: showCancel(chat.turnState),
		});
		if (action === null) return;
		event.preventDefault();
		if (action === 'close-menu') closeWorkspaceMenu();
		else if (action === 'close-palette') closePalette();
		else if (action === 'cancel-turn') cancelTurn();
		else if (action === 'open-palette') openPalette();
		else openSettings();
	}

	// Feed every daemon event into the timeline for the lifetime of the shell,
	// and start first-run detection (TD-1101) — the wizard probes setup state
	// after each handshake and opens when no API key is stored. The doctor
	// subscription (TD-1104) listens for diagnostics reports; the decisions
	// store (TD-1202) collects decision_logged events for its panel.
	onMount(() => {
		const offTimeline = onEvent(push);
		const offWizard = startOnboarding();
		const offDoctor = startDoctor();
		const offDecisions = startDecisions();
		const offSettings = startSettings();
		return () => {
			offTimeline();
			offWizard();
			offDoctor();
			offDecisions();
			offSettings();
		};
	});

</script>

<svelte:window onkeydown={onGlobalKeydown} />

<header class="shell-header">
	<span class="shell-title">TST Desk</span>
	<TitleBar />
	<span class="shell-spacer"></span>
	<!-- Command palette (TD-1707) — also ⌘K. The title is how the shortcut is
	     discovered, the same way the gear announces ⌘, (TD-1609). -->
	<button
		class="shell-gear"
		type="button"
		title="Commands (⌘K)"
		aria-label="Open command palette"
		onclick={openPalette}><Icon name="search" size={16} /></button
	>
	<!-- Review the session's decisions (TD-1202) at any time. -->
	<button
		class="shell-gear"
		type="button"
		title="Decisions"
		aria-label="Open decisions ledger"
		onclick={openDecisions}><Icon name="scroll" size={16} /></button
	>
	<!-- Run the doctor (TD-1104) at any time. -->
	<button
		class="shell-gear"
		type="button"
		title="Doctor"
		aria-label="Run doctor diagnostics"
		onclick={runDoctor}><Icon name="stethoscope" size={16} /></button
	>
	<!-- Settings (TD-1703) — also ⌘,. The wizard is first-run only. -->
	<button
		class="shell-gear"
		type="button"
		title="Settings (⌘,)"
		aria-label="Open settings"
		onclick={() => openSettings()}><Icon name="settings" size={16} /></button
	>
	<ConnectionBanner />
</header>

<NotificationBanner />

<div class="shell-body">
	<!-- Session rail (TD-1701): collapsible session list at the left edge. -->
	<SessionRail />
	<SplitPane>
		{#snippet left()}
			<section class="pane-chat" aria-label="Chat pane"><ChatPane /></section>
		{/snippet}
		{#snippet right()}
			<section class="pane-activity" aria-label="Activity and stack pane">
				<div class="pane-tabs" role="tablist" aria-label="Right pane views">
					<button
						role="tab"
						aria-selected={rightPane.tab === 'activity'}
						class="tab"
						class:tab-active={rightPane.tab === 'activity'}
						onclick={() => showRightPane('activity')}
					>
						Activity
					</button>
					<button
						role="tab"
						aria-selected={rightPane.tab === 'stack'}
						class="tab"
						class:tab-active={rightPane.tab === 'stack'}
						onclick={() => showRightPane('stack')}
					>
						Stack
					</button>
				</div>
				{#if rightPane.tab === 'activity'}
					<ActivityTimeline />
				{:else}
					<StackPanel />
				{/if}
			</section>
		{/snippet}
	</SplitPane>
</div>

<ApprovalBar />
<ToastStack />
<WizardPane />
<DoctorPane />
<DecisionsPane />
<SettingsPane />
<CommandPalette />

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
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-text);
	}

	/* Push the connection banner to the right edge of the shell header. */
	.shell-spacer {
		flex: 1;
	}

	/* Header affordances: decisions (TD-1202), doctor (TD-1104), wizard (TD-1101). */
	.shell-gear {
		display: inline-flex;
		align-items: center;
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

	/* The split pane shares the flex line with the rail (TD-1701): grow into
	   what the rail leaves, never push past it. */
	.shell-body > :global(.splitpane) {
		flex: 1;
		min-width: 0;
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
