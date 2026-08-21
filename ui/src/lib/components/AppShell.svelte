<script lang="ts">
	// Application shell: two-pane workspace layout using only design tokens.
	// Left = chat pane, right = activity pane. The title bar (TD-1006) and
	// connection banner (TD-1003) sit in the shell header so daemon/socket
	// state is visible at all times. The activity pane hosts the activity
	// timeline (TD-1005), fed live from the daemon event stream, the files
	// pane (TD-1705), the work diffs stack (TD-3203), the resolved-stack
	// panel (TD-1201), and the usage and cost pane (TD-1706) behind an
	// Activity | Files | Work | Stack | Usage tab strip.
	// Failure notices (TD-1008) render as banners under the header (blocking)
	// or toasts bottom-right (transient); the footer hosts pending approval
	// cards (TD-1007) and memory proposals (TD-2402).
	import { onMount } from 'svelte';
	import SplitPane from './SplitPane.svelte';
	import SessionRail from './SessionRail.svelte';
	import ConnectionBanner from '../ConnectionBanner.svelte';
	import ActivityTimeline from './ActivityTimeline.svelte';
	import FilesPanel from './FilesPanel.svelte';
	import WorkPanel from './WorkPanel.svelte';
	import StackPanel from './StackPanel.svelte';
	import UsagePanel from './UsagePanel.svelte';
	import ApprovalBar from './ApprovalBar.svelte';
	import MemoryProposalBar from './MemoryProposalBar.svelte';
	import { onEvent } from '../connection-status.svelte.js';
	import { push } from '../timeline-store.svelte.js';
	import ChatPane from './chat/ChatPane.svelte';
	import ProjectPane from './ProjectPane.svelte';
	import ArtifactPane from './ArtifactPane.svelte';
	import { projects } from '../projects.svelte.js';
	import { startArtifacts, bindArtifacts } from '../artifacts.svelte.js';
	import TitleBar from './TitleBar.svelte';
	import NotificationBanner from '../NotificationBanner.svelte';
	import ToastStack from '../ToastStack.svelte';
	import WizardPane from './WizardPane.svelte';
	import DoctorPane from './DoctorPane.svelte';
	import DecisionsPane from './DecisionsPane.svelte';
	import SettingsPane from './SettingsPane.svelte';
	import CommandPalette from './CommandPalette.svelte';
	import Icon from './Icon.svelte';
	import { start as startOnboarding, onboarding, closeWizard } from '../onboarding.svelte.js';
	import { startSettings, openSettings, closeSettings, settings } from '../settings.svelte.js';
	import { startDoctor, runDoctor, closeDoctor, doctor } from '../doctor.svelte.js';
	import { startDecisions, openDecisions, closeDecisions, decisions } from '../decisions.svelte.js';
	import { palette, openPalette, closePalette } from '../palette-store.svelte.js';
	import { rightPane, showRightPane } from '../right-pane.svelte.js';
	import { startUsage, refreshUsage, usage } from '../usage.svelte.js';
	import { startStack, refreshStack } from '../stack-store.svelte.js';
	import { startOsNotify, createTauriOsNotifyBridge } from '../os-notify.svelte.js';
	import { isTauri } from '../open-file';
	import { session } from '../session-status.svelte.js';
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
		else if (action === 'close-modal') closeTopModal();
		else if (action === 'cancel-turn') cancelTurn();
		else if (action === 'open-palette') openPalette();
		else openSettings();
	}

	// Top-most first, matching DOM order at the same --z-modal (settings is
	// last among the panes, so it paints above decisions/doctor/wizard).
	function closeTopModal(): void {
		if (settings.open) closeSettings();
		else if (decisions.open) closeDecisions();
		else if (doctor.open) closeDoctor();
		else if (onboarding.open) closeWizard();
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
		const offUsage = startUsage();
		// TD-1204: subscribe for the window's life, not the tab's. The
		// panel is only mounted on Stack; a reply with no subscriber is
		// dropped and the pane stays on "No instruction stack yet."
		const offStack = startStack();
		const offOsNotify = startOsNotify(isTauri() ? createTauriOsNotifyBridge() : undefined);
		const offArtifacts = startArtifacts();
		return () => {
			offTimeline();
			offWizard();
			offDoctor();
			offDecisions();
			offSettings();
			offUsage();
			offStack();
			offOsNotify();
			offArtifacts();
		};
	});

	// Refresh when Stack is the visible tab and when the bound session
	// changes under it. Palette "Stack" and the tab button both go
	// through `showRightPane`, so one effect covers both.
	$effect(() => {
		if (rightPane.tab !== 'stack') return;
		void session.sessionId;
		refreshStack();
	});

	$effect(() => {
		if (projects.surface !== 'artifacts') return;
		bindArtifacts(session.sessionId, session.workspacePath);
	});

	// The usage pane (TD-1706) reads the audit store, which the live event
	// stream does not update — so it loads when the tab is first opened
	// rather than polling behind a tab nobody is looking at.
	function showUsage(): void {
		showRightPane('usage');
		if (!usage.loaded) refreshUsage();
	}

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
	<!-- Settings is reached from the rail's account anchor (TD-1712) or ⌘,;
	     the header no longer buries it behind a gear. -->
	<ConnectionBanner />
</header>

<NotificationBanner />

<div class="shell-body">
	<!-- Session rail (TD-1701): collapsible session list at the left edge. -->
	<SessionRail />
	<SplitPane>
		{#snippet left()}
			<section
				class="pane-chat"
				aria-label={projects.surface === 'projects'
					? 'Projects'
					: projects.surface === 'artifacts'
						? 'Artifacts'
						: 'Chat pane'}
			>
				<!-- Chat stays mounted when another surface is showing so the
				     store subscription is not torn down (TD-2801). -->
				<div
					class="pane-layer"
					class:pane-hidden={projects.surface !== 'home'}
					aria-hidden={projects.surface !== 'home'}
				>
					<ChatPane />
				</div>
				{#if projects.surface === 'projects'}
					<div class="pane-layer"><ProjectPane /></div>
				{:else if projects.surface === 'artifacts'}
					<div class="pane-layer"><ArtifactPane /></div>
				{/if}
			</section>
		{/snippet}
		{#snippet right()}
			<section class="pane-activity" aria-label="Activity, files, work, stack, and usage pane">
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
					<!-- Files (TD-1705): the same event stream, folded by path. -->
					<button
						role="tab"
						aria-selected={rightPane.tab === 'files'}
						class="tab"
						class:tab-active={rightPane.tab === 'files'}
						onclick={() => showRightPane('files')}
					>
						Files
					</button>
					<button
						role="tab"
						aria-selected={rightPane.tab === 'work'}
						class="tab"
						class:tab-active={rightPane.tab === 'work'}
						onclick={() => showRightPane('work')}
					>
						Work
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
					<!-- Usage (TD-1706): the audit store's rollups and exports. -->
					<button
						role="tab"
						aria-selected={rightPane.tab === 'usage'}
						class="tab"
						class:tab-active={rightPane.tab === 'usage'}
						onclick={showUsage}
					>
						Usage
					</button>
				</div>
				{#if rightPane.tab === 'activity'}
					<ActivityTimeline />
				{:else if rightPane.tab === 'files'}
					<FilesPanel />
				{:else if rightPane.tab === 'work'}
					<WorkPanel />
				{:else if rightPane.tab === 'usage'}
					<UsagePanel />
				{:else}
					<StackPanel />
				{/if}
			</section>
		{/snippet}
	</SplitPane>
</div>

<ApprovalBar />
{#if projects.surface === 'home'}
	<MemoryProposalBar />
{/if}
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

	/* Header affordances: decisions (TD-1202), doctor (TD-1104). */
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

	.pane-layer {
		height: 100%;
		min-height: 0;
	}

	.pane-hidden {
		display: none;
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
	.pane-activity > :global(.files-panel),
	.pane-activity > :global(.work-panel),
	.pane-activity > :global(.stack-panel) {
		flex: 1;
		min-height: 0;
	}
</style>
