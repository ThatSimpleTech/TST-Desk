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
	import ScreenPane from './ScreenPane.svelte';
	import PreviewPane from './PreviewPane.svelte';
	import PlanPane from './PlanPane.svelte';
	import ApprovalBar from './ApprovalBar.svelte';
	import MemoryProposalBar from './MemoryProposalBar.svelte';
	import { onEvent } from '../connection-status.svelte.js';
	import { entries, push } from '../timeline-store.svelte.js';
	import { inspectorCounts, tabCount } from '../inspector';
	import { selectRow, setPickForNewSession, visibleRows } from '../sessions.svelte.js';
	import ChatPane from './chat/ChatPane.svelte';
	import ProjectPane from './ProjectPane.svelte';
	import ArtifactPane from './ArtifactPane.svelte';
	import ScheduledPane from './ScheduledPane.svelte';
	import { projects } from '../projects.svelte.js';
	import { startArtifacts, bindArtifacts } from '../artifacts.svelte.js';
	import { startScheduled, refreshJobs } from '../scheduled.svelte.js';
	import TitleBar from './TitleBar.svelte';
	import NotificationBanner from '../NotificationBanner.svelte';
	import ToastStack from '../ToastStack.svelte';
	import WizardPane from './WizardPane.svelte';
	import DoctorPane from './DoctorPane.svelte';
	import DecisionsPane from './DecisionsPane.svelte';
	import SettingsPane from './SettingsPane.svelte';
	import CuPermissionsPane from './CuPermissionsPane.svelte';
	import CommandPalette from './CommandPalette.svelte';
	import Icon from './Icon.svelte';
	import { start as startOnboarding, onboarding, closeWizard } from '../onboarding.svelte.js';
	import { startSettings, openSettings, closeSettings, settings } from '../settings.svelte.js';
	import {
		startCuPermissions,
		closeCuPermissions,
		cuPermissions,
	} from '../cu-permissions.svelte.js';
	import { startDoctor, runDoctor, closeDoctor, doctor } from '../doctor.svelte.js';
	import { startDecisions, openDecisions, closeDecisions, decisions } from '../decisions.svelte.js';
	import { palette, openPalette, closePalette } from '../palette-store.svelte.js';
	import { rightPane, showRightPane, type RightPaneTab } from '../right-pane.svelte.js';
	import { startUsage, refreshUsage, usage } from '../usage.svelte.js';
	import { startStack, refreshStack } from '../stack-store.svelte.js';
	import { startScreen, screen } from '../screen.svelte.js';
	import { startCuIndicators } from '../screen-indicator.svelte.js';
	import { startDesign, toggleDesign } from '../design.svelte.js';
	import { screenTabVisible } from '../screen';
	import { startOsNotify, createTauriOsNotifyBridge } from '../os-notify.svelte.js';
	import { startCloseHint } from '../close-hint';
	import { startCoworkerIndicator } from '../coworker-indicator.svelte.js';
	import { isTauri } from '../open-file';
	import { session } from '../session-status.svelte.js';
	import { startCuKill, setCuKill } from '../cu-kill.svelte.js';
	import { startGrok } from '../grok.svelte.js';
	import { startVoice } from '../voice.svelte.js';
	import { resolveShortcut, sessionSlot } from '../shortcuts';
	import { groupRowsByRecency, railOrder, sessionIdAtSlot, stepSessionId } from '../rail';
	import { chat, cancelTurn } from '../chat-store.svelte.js';
	import { showCancel } from '../chat-store';
	import { workspaces, closeWorkspaceMenu } from '../workspaces.svelte.js';
	import RemoteConnectForm from './RemoteConnectForm.svelte';
	import { narrowMediaQuery } from '../layout-mode';

	// TD-3701: one AppShell. Below 640px the CSS hides rail + inspector;
	// these flags only reveal them again. Wide viewports ignore the flags.
	let narrow = $state(false);
	let showRail = $state(false);
	let showInspector = $state(false);

	// Global shortcuts (TD-1609): Esc peels layers (menu → palette → modal →
	// turn), ⌘, opens settings (TD-1703), ⌘K the command palette (TD-1707).
	// The mapping itself is pure — see shortcuts.ts. One listener, one layer
	// order: no pane installs a keydown handler of its own.
	function onGlobalKeydown(event: KeyboardEvent): void {
		const action = resolveShortcut(
			{
				key: event.key,
				metaKey: event.metaKey,
				ctrlKey: event.ctrlKey,
				shiftKey: event.shiftKey,
				altKey: event.altKey,
			},
			{
				workspaceMenuOpen: workspaces.menuOpen,
				paletteOpen: palette.open,
				modalOpen:
					onboarding.open ||
					doctor.open ||
					decisions.open ||
					settings.open ||
					cuPermissions.open ||
					palette.open,
				turnLive: showCancel(chat.turnState),
			},
		);
		if (action === null) return;
		event.preventDefault();
		if (action === 'close-menu') closeWorkspaceMenu();
		else if (action === 'close-palette') closePalette();
		else if (action === 'close-modal') closeTopModal();
		else if (action === 'cancel-turn') cancelTurn();
		else if (action === 'open-palette') openPalette();
		else if (action === 'toggle-design') {
			if (toggleDesign()) showRightPane('screen');
		} else if (action === 'stop-computer-use') setCuKill(true);
		else if (action === 'next-session' || action === 'prev-session') {
			const id = stepSessionId(sessionOrder(), chat.sessionId, action === 'next-session' ? 1 : -1);
			if (id !== null) selectRow(id);
		} else if (action === 'pick-session') {
			const slot = sessionSlot({
				key: event.key,
				metaKey: event.metaKey,
				ctrlKey: event.ctrlKey,
				shiftKey: event.shiftKey,
				altKey: event.altKey,
			});
			const id = slot === null ? null : sessionIdAtSlot(sessionOrder(), slot);
			if (id !== null) selectRow(id);
		} else openSettings();
	}

	// The rail's rows in the order it draws them (navigation round, 2026-09):
	// what ⌘⌥↑ / ⌘⌥↓ step through and what ⌘1…⌘9 index into. Read at the
	// keypress rather than derived, so the shell keeps no copy of the rail.
	function sessionOrder(): string[] {
		return railOrder(groupRowsByRecency(visibleRows()));
	}

	// Top-most first, matching DOM order at the same --z-modal (settings is
	// last among the panes, so it paints above decisions/doctor/wizard).
	function closeTopModal(): void {
		if (cuPermissions.open) closeCuPermissions();
		else if (settings.open) closeSettings();
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
		const offCuPerms = startCuPermissions();
		const offUsage = startUsage();
		// TD-4825: a "+" press with no sessions to anchor on opens the
		// workspace picker instead of doing nothing.
		setPickForNewSession(async () => {
			const { open } = await import('@tauri-apps/plugin-dialog');
			const chosen = await open({ directory: true, multiple: false });
			return typeof chosen === 'string' ? chosen : null;
		});
		// TD-1204: subscribe for the window's life, not the tab's. The
		// panel is only mounted on Stack; a reply with no subscriber is
		// dropped and the pane stays on "No instruction stack yet."
		const offStack = startStack();
		const offOsNotify = startOsNotify(isTauri() ? createTauriOsNotifyBridge() : undefined);
		const offArtifacts = startArtifacts();
		const offScheduled = startScheduled();
		const offScreen = startScreen();
		const offCuIndicators = startCuIndicators();
		const offDesign = startDesign();
		const offCoworker = startCoworkerIndicator();
		const offCuKill = startCuKill();
		const offGrok = startGrok();
		const offVoice = startVoice();
		let offCloseHint = () => {};
		void startCloseHint().then((off) => {
			offCloseHint = off;
		});
		const mq = window.matchMedia(narrowMediaQuery());
		const syncNarrow = () => {
			narrow = mq.matches;
			if (!mq.matches) {
				showRail = false;
				showInspector = false;
			}
		};
		syncNarrow();
		mq.addEventListener('change', syncNarrow);
		return () => {
			offTimeline();
			offWizard();
			offDoctor();
			offDecisions();
			offSettings();
			offCuPerms();
			offUsage();
			offStack();
			offOsNotify();
			offArtifacts();
			offScheduled();
			offScreen();
			offCuIndicators();
			offDesign();
			offCoworker();
			offCuKill();
			offGrok();
			offVoice();
			offCloseHint();
			mq.removeEventListener('change', syncNarrow);
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

	$effect(() => {
		if (projects.surface !== 'scheduled') return;
		refreshJobs(session.workspacePath);
	});

	// The usage pane (TD-1706) reads the audit store, which the live event
	// stream does not update — so it loads when the tab is first opened
	// rather than polling behind a tab nobody is looking at.
	function showUsage(): void {
		showRightPane('usage');
		if (!usage.loaded) refreshUsage();
	}

	let showScreenTab = $derived(
		screenTabVisible({
			boundSessionId: screen.boundSessionId,
			sessionId: session.sessionId,
			hasFrame: screen.hasFrame,
			hasCuTool: screen.hasCuTool,
		}),
	);

	// First computer-use turn in a session opens Screen so the glow and
	// frame are watchable. Later tab changes are the user's.
	let openedScreenFor = $state<string | null>(null);
	$effect(() => {
		const id = session.sessionId;
		if (id === null) {
			openedScreenFor = null;
			return;
		}
		if (showScreenTab && openedScreenFor !== id) {
			openedScreenFor = id;
			showRightPane('screen');
		}
	});

	// Three tabs you reach for every turn stay on the strip; the rest wait
	// under More. Eight tabs at the same weight was a row nobody could scan.
	// When a More view is showing, the More button takes that view's name.
	const PRIMARY_TABS: readonly (readonly [RightPaneTab, string])[] = [
		['activity', 'Activity'],
		['files', 'Files'],
		['work', 'Work'],
	];
	let moreTabs = $derived<(readonly [RightPaneTab, string])[]>([
		['stack', 'Stack'],
		['usage', 'Usage'],
		...(showScreenTab ? ([['screen', 'Screen']] as const) : []),
		['preview', 'Preview'],
		['plan', 'Plan'],
	]);
	let moreOpen = $state(false);
	let moreRoot: HTMLElement | undefined = $state();
	let activeMore = $derived(moreTabs.find(([id]) => id === rightPane.tab) ?? null);

	// Files and Work carry their counts on the strip, from the same fold the
	// panes render from, so "did the agent write anything?" needs no click.
	let counts = $derived(inspectorCounts(entries));

	function badgeFor(id: RightPaneTab): string | null {
		if (id === 'files') return tabCount(counts.files);
		if (id === 'work') return tabCount(counts.work);
		return null;
	}

	function pickTab(id: RightPaneTab): void {
		moreOpen = false;
		if (id === 'usage') showUsage();
		else showRightPane(id);
	}

	function onWindowClick(event: MouseEvent): void {
		if (!moreOpen) return;
		if (moreRoot !== undefined && event.target instanceof Node && moreRoot.contains(event.target)) {
			return;
		}
		moreOpen = false;
	}

	function onMoreKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || !moreOpen) return;
		// The menu is the layer to peel; the global Esc must not also cancel
		// a live turn underneath it.
		event.preventDefault();
		event.stopPropagation();
		moreOpen = false;
	}
</script>

<svelte:window onkeydown={onGlobalKeydown} onclick={onWindowClick} />

<header class="shell-header">
	<span class="shell-title">TST Desk</span>
	<!-- Connection state (TD-1003) sits beside the wordmark: nothing at all
	     while connected, a labelled pill for anything else. -->
	<ConnectionBanner />
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
	{#if narrow}
		<button
			class="shell-gear"
			type="button"
			title="Sessions"
			aria-label="Show sessions"
			aria-pressed={showRail}
			onclick={() => (showRail = !showRail)}><Icon name="panel-left" size={16} /></button
		>
		<button
			class="shell-gear"
			type="button"
			title="Inspector"
			aria-label="Show inspector"
			aria-pressed={showInspector}
			onclick={() => (showInspector = !showInspector)}><Icon name="panel-right" size={16} /></button
		>
	{/if}
	<!-- Settings is reached from the rail's account anchor (TD-1712) or ⌘,;
	     the header no longer buries it behind a gear. -->
</header>

<NotificationBanner />

<div
	class="shell-body"
	class:shell-show-rail={showRail}
	class:shell-show-inspector={showInspector}
>
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
						: projects.surface === 'scheduled'
							? 'Scheduled'
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
				{:else if projects.surface === 'scheduled'}
					<div class="pane-layer"><ScheduledPane /></div>
				{/if}
			</section>
		{/snippet}
		{#snippet right()}
			<section class="pane-activity" aria-label="Activity, files, work, stack, usage, and screen pane">
				<div class="pane-tabs" role="tablist" aria-label="Right pane views">
					<!-- Activity (TD-1005), Files (TD-1705: the same event stream,
					     folded by path), Work (TD-3203). -->
					{#each PRIMARY_TABS as [id, label] (id)}
						{@const badge = badgeFor(id)}
						<button
							role="tab"
							type="button"
							aria-selected={rightPane.tab === id}
							class="tab"
							class:tab-active={rightPane.tab === id}
							onclick={() => pickTab(id)}
						>
							{label}
							{#if badge !== null}
								<span class="tab-count">{badge}</span>
							{/if}
						</button>
					{/each}
					<!-- Stack (TD-1201), Usage (TD-1706: the audit store's rollups
					     and exports), Screen, Preview and Plan wait under More. -->
					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<div class="more" bind:this={moreRoot} onkeydown={onMoreKeydown}>
						<button
							class="tab tab-more"
							class:tab-active={activeMore !== null}
							type="button"
							aria-haspopup="menu"
							aria-expanded={moreOpen}
							onclick={() => (moreOpen = !moreOpen)}
						>
							{activeMore === null ? 'More' : activeMore[1]}
							<span class="tab-caret" aria-hidden="true"><Icon name="chevron-down" size={12} /></span>
						</button>
						{#if moreOpen}
							<div class="more-menu" role="menu" aria-label="More views">
								{#each moreTabs as [id, label] (id)}
									<button
										class="more-item"
										class:more-item-active={rightPane.tab === id}
										role="menuitemradio"
										aria-checked={rightPane.tab === id}
										type="button"
										onclick={() => pickTab(id)}
									>
										{label}
									</button>
								{/each}
							</div>
						{/if}
					</div>
				</div>
				{#if rightPane.tab === 'activity'}
					<ActivityTimeline />
				{:else if rightPane.tab === 'files'}
					<FilesPanel />
				{:else if rightPane.tab === 'work'}
					<WorkPanel />
				{:else if rightPane.tab === 'usage'}
					<UsagePanel />
				{:else if rightPane.tab === 'screen'}
					<ScreenPane />
				{:else if rightPane.tab === 'preview'}
					<PreviewPane />
				{:else if rightPane.tab === 'plan'}
					<PlanPane />
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
<CuPermissionsPane />
<CommandPalette />
<RemoteConnectForm />

<style>
	.shell-header {
		display: flex;
		align-items: center;
		gap: var(--space-4);
		height: var(--space-12);
		padding: 0 var(--space-6);
		border-bottom: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		flex-shrink: 0;
	}

	.shell-title {
		font-family: var(--font-display);
		font-size: var(--text-base);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-ink);
	}

	/* Push the header's tool buttons to the right edge. */
	.shell-spacer {
		flex: 1;
	}

	/* Header affordances: palette (TD-1707), decisions (TD-1202), doctor
	   (TD-1104), and the narrow-viewport rail/inspector toggles (TD-3701). */
	.shell-gear {
		display: inline-flex;
		align-items: center;
		border: none;
		background: transparent;
		font-size: var(--text-base);
		color: var(--color-ink-secondary);
		cursor: pointer;
		padding: var(--space-1) var(--space-2);
		border-radius: var(--radius-md);
		line-height: 1;
	}

	.shell-gear:hover {
		background: var(--color-sunken);
		color: var(--color-ink);
	}

	.shell-gear[aria-pressed='true'] {
		color: var(--color-accent);
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
		background: var(--color-ground);
		color: var(--color-ink);
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
		background: var(--color-sunken);
		display: flex;
		flex-direction: column;
	}

	.pane-tabs {
		display: flex;
		align-items: flex-end;
		gap: var(--space-1);
		padding: var(--space-2) var(--space-4) 0;
		border-bottom: var(--border-width) solid var(--color-hairline);
		flex-shrink: 0;
	}

	.tab {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		padding: var(--space-1) var(--space-3);
		border: none;
		border-bottom: 2px solid transparent;
		background: none;
		color: var(--color-ink-muted);
		font-size: var(--text-sm);
		cursor: pointer;
		transition: color var(--transition-fast);
	}

	.tab:hover {
		color: var(--color-ink);
	}

	.tab:focus-visible {
		outline-offset: -2px;
		border-radius: var(--radius-sm);
	}

	.tab-active {
		color: var(--color-ink);
		font-weight: var(--weight-semibold);
		border-bottom-color: var(--color-accent);
	}

	.tab-caret {
		display: inline-flex;
		color: var(--color-ink-muted);
	}

	/* Count pill: quiet enough to sit on an inactive tab, readable on the
	   active one. Tabular digits keep 9 → 10 from nudging the strip. */
	.tab-count {
		display: inline-flex;
		align-items: center;
		min-width: 1.25rem;
		justify-content: center;
		padding: 0 var(--space-1);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		line-height: 1.25rem;
		color: var(--color-ink-secondary);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		font-variant-numeric: tabular-nums;
	}

	.more {
		position: relative;
		margin-left: auto;
	}

	.more-menu {
		position: absolute;
		top: calc(100% + var(--space-1));
		right: 0;
		z-index: 5;
		min-width: 10rem;
		display: flex;
		flex-direction: column;
		padding: var(--space-1);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-lg);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.more-item {
		width: 100%;
		padding: var(--space-1) var(--space-2);
		border: 0;
		background: transparent;
		border-radius: var(--radius-sm);
		text-align: left;
		font-size: var(--text-sm);
		color: var(--color-ink);
		cursor: pointer;
	}

	.more-item:hover {
		background: var(--color-sunken);
	}

	.more-item-active {
		color: var(--color-accent);
		font-weight: var(--weight-semibold);
	}

	.pane-activity > :global(.timeline),
	.pane-activity > :global(.files-panel),
	.pane-activity > :global(.work-panel),
	.pane-activity > :global(.stack-panel),
	.pane-activity > :global(.screen-pane) {
		flex: 1;
		min-height: 0;
	}

	/* TD-3701: one pane on a narrow viewport (chat + approval). Rail and
	   inspector stay in the DOM so optional reveal is a class, not a second
	   shell. Keep in sync with NARROW_VIEWPORT_PX / narrowMediaQuery(). */
	@media (max-width: 639px) {
		.shell-header {
			overflow-x: auto;
		}

		.shell-body:not(.shell-show-rail) :global(.rail) {
			display: none;
		}

		.shell-body:not(.shell-show-inspector) :global(.splitpane) {
			grid-template-columns: 1fr !important;
		}

		.shell-body:not(.shell-show-inspector) :global(.splitpane .right),
		.shell-body:not(.shell-show-inspector) :global(.splitpane .divider) {
			display: none;
		}
	}
</style>
