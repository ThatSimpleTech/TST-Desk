<script lang="ts">
	// One file the agent sent (TD-5144, design §6.4). Kind icon on the left;
	// the name, ellipsized in the middle so the extension stays on screen;
	// the size or the download's progress; on the right the button the phase
	// calls for and a `…` menu for the rest. A 3 px bar on the bottom edge
	// tracks bytes while a download runs, checks or waits.
	//
	// The state is the download store's (downloads.svelte.ts): the first
	// render probes the platform, so after a restart the card shows what is
	// really on this device. The copy and the button matrix are pure
	// (sent-files.ts); this file only lays them out.
	import { downloadPlatform, runAction, stateFor } from "../../downloads.svelte.js";
	import type { IconName } from "../../icons";
	import {
		actionLabel,
		apkInfoLine,
		apkNotes,
		apkWarnings,
		cardLabel,
		fileActions,
		fileKind,
		nameParts,
		statusLine,
		type FileAction,
		type FileKind,
		type SentFileRef,
	} from "../../sent-files";
	import Icon from "../Icon.svelte";

	let { file, compact = false }: { file: SentFileRef; compact?: boolean } = $props();

	const platform = downloadPlatform();
	const view = $derived(stateFor(file));
	const kind = $derived(fileKind(file.name, file.mime));
	const parts = $derived(nameParts(file.name));
	const apk = $derived(
		platform === "android" && (view.phase === "done" || view.phase === "saved") ? view.apk : null,
	);
	const warnings = $derived(apk === null ? [] : apkWarnings(apk));
	const notes = $derived(apk === null ? [] : apkNotes(apk, view.canInstall));
	const actions = $derived(
		fileActions(platform, kind, view.phase, {
			local: view.local,
			warning: warnings.length > 0,
			partial: view.partial,
			reveal: view.reveal,
		}),
	);
	const status = $derived(statusLine(file, view, platform));
	const showBar = $derived(
		view.phase === "downloading" || view.phase === "verifying" || view.phase === "paused",
	);
	const percent = $derived(
		view.phase === "verifying" ? 100 : file.size > 0 ? Math.min(100, (view.have / file.size) * 100) : 0,
	);

	const KIND_ICONS: Record<FileKind, IconName> = {
		apk: "package",
		image: "file",
		pdf: "file",
		archive: "archive",
		video: "file",
		audio: "file",
		text: "file",
		program: "terminal",
		other: "file",
	};

	let menuOpen = $state(false);
	let busy = $state(false);
	let menuRoot: HTMLDivElement | null = $state(null);

	async function run(action: FileAction): Promise<void> {
		menuOpen = false;
		busy = true;
		try {
			await runAction(file, action);
		} finally {
			busy = false;
		}
	}

	function closeMenu(event: MouseEvent): void {
		if (!menuOpen) return;
		const target = event.target as Node | null;
		if (target !== null && menuRoot?.contains(target)) return;
		menuOpen = false;
	}
</script>

<svelte:window
	onclick={closeMenu}
	onkeydown={(e) => {
		if (e.key === "Escape") menuOpen = false;
	}}
/>

<div
	class="file-card"
	class:compact
	class:touch={platform === "android"}
	role="group"
	aria-label={cardLabel(file)}
	data-phase={view.phase}
>
	<span class="kind" aria-hidden="true"><Icon name={KIND_ICONS[kind]} size={compact ? 18 : 22} /></span>
	<div class="body">
		<span class="name" title={file.name}
			><span class="head">{parts.head}</span>{#if parts.tail !== ""}<span class="tail">{parts.tail}</span
				>{/if}</span
		>
		<span class="status" aria-live="polite">{status}</span>
		{#if apk !== null}
			<span class="apk">{apkInfoLine(apk)}</span>
			{#each warnings as warning (warning)}
				<span class="warn">{warning}</span>
			{/each}
			{#each notes as note (note)}
				<span class="note">{note}</span>
			{/each}
		{/if}
		{#if compact && file.caption !== null}
			<span class="caption one-line">{file.caption}</span>
		{/if}
	</div>
	{#if actions.primary !== null || actions.menu.length > 0}
		<div class="actions" bind:this={menuRoot}>
			{#if actions.primary !== null}
				<button
					class="primary"
					type="button"
					disabled={busy}
					onclick={() => void run(actions.primary as FileAction)}
				>
					{#if actions.primary === "download" || actions.primary === "download-install" || actions.primary === "download-open"}<Icon
							name="download"
							size={14}
						/>{/if}
					{actionLabel(actions.primary)}
				</button>
			{/if}
			{#if actions.menu.length > 0}
				<button
					class="more"
					type="button"
					aria-label={`More for ${file.name}`}
					aria-haspopup="menu"
					aria-expanded={menuOpen}
					onclick={() => (menuOpen = !menuOpen)}
				>
					<Icon name="ellipsis" size={16} />
				</button>
				{#if menuOpen}
					<div class="menu" role="menu">
						{#each actions.menu as action (action)}
							<button class="item" type="button" role="menuitem" onclick={() => void run(action)}>
								{actionLabel(action)}
							</button>
						{/each}
					</div>
				{/if}
			{/if}
		</div>
	{/if}
	{#if showBar}
		<span class="bar" aria-hidden="true"><span class="fill" style:width={`${percent}%`}></span></span>
	{/if}
</div>

<style>
	.file-card {
		position: relative;
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-lifted);
		color: var(--color-ink);
		overflow: visible;
	}

	.file-card.compact {
		padding: var(--space-2) var(--space-3);
		gap: var(--space-2);
	}

	.kind {
		flex: 0 0 40px;
		width: 40px;
		height: 40px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: var(--radius-sm);
		background: var(--color-sunken);
		color: var(--color-ink-secondary);
	}

	.compact .kind {
		flex-basis: 28px;
		width: 28px;
		height: 28px;
	}

	.body {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	/* Middle ellipsis: the head gives way, the tail (extension) never does. */
	.name {
		display: flex;
		min-width: 0;
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		white-space: nowrap;
	}

	.head {
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.tail {
		flex-shrink: 0;
	}

	.status,
	.apk,
	.note {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.warn {
		font-size: var(--text-xs);
		color: var(--color-warn);
	}

	.caption {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.one-line {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.actions {
		position: relative;
		display: flex;
		align-items: center;
		gap: var(--space-1);
		flex-shrink: 0;
	}

	.primary,
	.more,
	.item {
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
		cursor: pointer;
	}

	.primary {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		padding: var(--space-1) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
	}

	.primary:hover:not(:disabled),
	.more:hover,
	.item:hover {
		background: var(--color-sunken);
	}

	.primary:disabled {
		color: var(--color-ink-muted);
		cursor: default;
	}

	.more {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-1);
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--color-ink-muted);
	}

	.primary:focus-visible,
	.more:focus-visible,
	.item:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
	}

	.menu {
		position: absolute;
		top: calc(100% + var(--space-1));
		right: 0;
		z-index: 2;
		min-width: 11rem;
		display: flex;
		flex-direction: column;
		padding: var(--space-1);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-lifted);
		box-shadow: var(--shadow-md);
	}

	.item {
		text-align: left;
		padding: var(--space-2) var(--space-3);
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
	}

	/* The phone: every target at least 44 px. */
	.touch .primary,
	.touch .more,
	.touch .item {
		min-height: 44px;
		min-width: 44px;
	}

	.bar {
		position: absolute;
		left: 0;
		right: 0;
		bottom: 0;
		height: 3px;
		overflow: hidden;
		border-bottom-left-radius: var(--radius-md);
		border-bottom-right-radius: var(--radius-md);
		background: var(--color-sunken);
	}

	.fill {
		display: block;
		height: 100%;
		background: var(--color-accent);
		transition: width var(--transition-fast);
	}

	[data-phase="paused"] .fill {
		background: var(--color-ink-muted);
	}

	@media (prefers-reduced-motion: reduce) {
		.fill {
			transition: none;
		}
	}
</style>
