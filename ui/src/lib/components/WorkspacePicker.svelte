<script lang="ts">
	// Workspace name button and its recents menu (TD-1103).
	//
	// Split out of TitleBar so neither file crowds the ~400-line ceiling
	// (AGENTS §6). Presentational: the recents list and the open/closed state
	// stay in the workspaces store, which this only reads and dispatches to.
	import { session, workspaceName } from '../session-status.svelte.js';
	import {
		workspaces,
		visibleRecents,
		openRecent,
		hideRecent,
		toggleWorkspaceMenu,
		closeWorkspaceMenu,
		startWorkspaces,
	} from '../workspaces.svelte.js';
	import { onMount } from 'svelte';
	import Icon from './Icon.svelte';

	interface Props {
		/** Directory picker, injectable for tests. Defaults to the Tauri dialog plugin. */
		pickDirectory?: () => Promise<string | null>;
	}
	let { pickDirectory }: Props = $props();

	async function defaultPickDirectory(): Promise<string | null> {
		const { open } = await import('@tauri-apps/plugin-dialog');
		const chosen = await open({ directory: true, multiple: false });
		return typeof chosen === 'string' ? chosen : null;
	}

	async function pick(): Promise<void> {
		const path = await (pickDirectory ?? defaultPickDirectory)();
		if (path !== null) openRecent(path);
	}

	onMount(() => startWorkspaces());

	function onBackdropKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') closeWorkspaceMenu();
	}

	let displayName = $derived(
		session.workspacePath !== null ? workspaceName(session.workspacePath) : null,
	);
</script>

<!-- The name opens the recents menu (TD-1103 quick switch + per-entry remove);
     the folder entry in the menu is the picker. -->
<div class="ws-wrap">
	<button
		class="workspace"
		type="button"
		aria-haspopup="menu"
		aria-expanded={workspaces.menuOpen}
		onclick={toggleWorkspaceMenu}
		title={session.workspacePath ?? 'Open a workspace'}
	>
		<span class="folder"><Icon name="folder" size={12} /></span>
		<span>{displayName ?? 'Open workspace…'}</span>
		<span class="chevron" aria-hidden="true"><Icon name="chevron-down" size={10} /></span>
	</button>

	{#if workspaces.menuOpen}
		<button
			class="ws-backdrop"
			type="button"
			tabindex="-1"
			aria-hidden="true"
			onclick={closeWorkspaceMenu}
			onkeydown={onBackdropKeydown}
		></button>
		<div class="ws-menu" role="menu" aria-label="Recent workspaces">
			{#each visibleRecents() as recent (recent.path)}
				<div class="ws-row" role="none">
					<button
						class="ws-switch"
						type="button"
						role="menuitem"
						title={recent.path}
						onclick={() => openRecent(recent.path)}
					>
						<span class="ws-name">{workspaceName(recent.path)}</span>
						<span class="ws-path">{recent.path}</span>
					</button>
					<button
						class="ws-remove"
						type="button"
						role="menuitem"
						aria-label="Remove {workspaceName(recent.path)} from recents"
						title="Remove from recents"
						onclick={() => hideRecent(recent.path)}
					><Icon name="x" size={10} /></button>
				</div>
			{/each}
			{#if visibleRecents().length > 0}
				<div class="ws-sep" aria-hidden="true"></div>
			{/if}
			<button class="ws-open" type="button" role="menuitem" onclick={pick}>
				<Icon name="folder" size={12} /> Open folder…
			</button>
		</div>
	{/if}
</div>

<style>
	.ws-wrap {
		position: relative;
	}

	.workspace {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text);
		background: transparent;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		max-width: 20rem;
		overflow: hidden;
		white-space: nowrap;
	}

	.workspace:hover {
		background: var(--color-bg-subtle);
	}

	.folder {
		display: inline-flex;
		flex-shrink: 0;
		color: var(--color-text-secondary);
	}

	.chevron {
		display: inline-flex;
		color: var(--color-text-secondary);
	}

	.ws-backdrop {
		position: fixed;
		inset: 0;
		z-index: 19;
		background: transparent;
		border: 0;
		cursor: default;
	}

	.ws-menu {
		position: absolute;
		top: calc(100% + var(--space-1));
		left: 0;
		z-index: 20;
		min-width: 16rem;
		max-width: 26rem;
		background: var(--color-bg-raised);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-lg);
		padding: var(--space-1);
	}

	.ws-row {
		display: flex;
		align-items: center;
	}

	.ws-switch {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		flex: 1;
		min-width: 0;
		gap: 1px;
		background: transparent;
		border: 0;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		text-align: left;
	}

	.ws-switch:hover {
		background: var(--color-bg-subtle);
	}

	.ws-name {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text);
	}

	.ws-path {
		font-size: 10px;
		font-family: var(--font-mono);
		color: var(--color-text-secondary);
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ws-remove {
		background: transparent;
		border: 0;
		color: var(--color-text-muted);
		font-size: 10px;
		padding: var(--space-1);
		cursor: pointer;
		border-radius: var(--radius-sm);
		flex-shrink: 0;
	}

	.ws-remove:hover {
		color: var(--color-danger);
		background: var(--color-bg-subtle);
	}

	.ws-sep {
		border-top: 1px solid var(--color-border);
		margin: var(--space-1) 0;
	}

	.ws-open {
		display: block;
		width: 100%;
		background: transparent;
		border: 0;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-text);
		cursor: pointer;
		text-align: left;
	}

	.ws-open:hover {
		background: var(--color-bg-subtle);
	}
</style>
