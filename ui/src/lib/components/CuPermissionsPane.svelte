<script lang="ts">
	// Computer-use permission explanation (TD-3302). Same copy for the
	// first-run panel and the Settings reopen. macOS names only — Windows
	// copy is TD-3303.
	import {
		cuPermissions,
		closeCuPermissions,
		openCuPermissions,
		openSystemSettings,
		retryCuPermissions,
	} from '../cu-permissions.svelte.js';
	import Icon from './Icon.svelte';

	interface Props {
		variant?: 'dialog' | 'settings';
	}

	let { variant = 'dialog' }: Props = $props();

	const screenOk = $derived(cuPermissions.screenRecording);
	const accessOk = $derived(cuPermissions.accessibility);
</script>

{#if variant === 'settings'}
	<div class="keep-running">
		<p class="keep-title">Computer use permissions</p>
		<button class="btn" type="button" onclick={() => openCuPermissions()}
			>Show explanation</button
		>
	</div>
	<p class="hint">Reopens the Screen Recording and Accessibility explanation.</p>
{:else if cuPermissions.open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div
		class="overlay"
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label="Computer use permissions"
		onclick={(e) => {
			if (e.target === e.currentTarget) closeCuPermissions();
		}}
	>
		<div class="pane">
			<div class="head">
				<h1 class="title">Computer use permissions</h1>
				<button class="close" type="button" aria-label="Close" onclick={closeCuPermissions}>
					<Icon name="x" size={14} />
				</button>
			</div>
			<p class="body">
				Desktop computer use needs two macOS permissions, granted to this app — not the sidecar.
				After you enable them, fully quit and reopen TST Desk; macOS caches the grant per-binary.
			</p>
			<ul class="perms">
				<li>
					<strong>Screen Recording</strong>
					— capture the desktop (screenshots).
					<span class="status" class:status--ok={screenOk} class:status--bad={!screenOk}>
						{screenOk ? 'granted' : 'denied'}
					</span>
				</li>
				<li>
					<strong>Accessibility</strong>
					— move the pointer, click, type, and scroll.
					<span class="status" class:status--ok={accessOk} class:status--bad={!accessOk}>
						{accessOk ? 'granted' : 'denied'}
					</span>
				</li>
			</ul>
			<div class="actions">
				<button
					class="btn"
					type="button"
					disabled={cuPermissions.screenRecordingUrl === ''}
					onclick={() => void openSystemSettings(cuPermissions.screenRecordingUrl)}
					>Screen Recording settings</button
				>
				<button
					class="btn"
					type="button"
					disabled={cuPermissions.accessibilityUrl === ''}
					onclick={() => void openSystemSettings(cuPermissions.accessibilityUrl)}
					>Accessibility settings</button
				>
				<button
					class="btn btn--primary"
					type="button"
					disabled={cuPermissions.probing}
					onclick={() => retryCuPermissions()}>Retry</button
				>
			</div>
			{#if cuPermissions.probing}
				<p class="hint" aria-live="polite">Checking permissions…</p>
			{:else if cuPermissions.granted}
				<p class="hint" aria-live="polite">Both permissions are granted. You can retry the turn.</p>
			{/if}
		</div>
	</div>
{/if}

<style>
	.overlay {
		position: fixed;
		inset: 0;
		z-index: var(--z-modal);
		display: grid;
		place-items: center;
		background: rgb(0 0 0 / 0.28);
	}

	.pane {
		width: min(34rem, 92vw);
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-lg);
		padding: var(--space-4);
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: var(--space-3);
	}

	.title {
		font-size: var(--text-md);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
		margin: 0;
	}

	.close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 24px;
		min-height: 24px;
		background: transparent;
		border: 0;
		color: var(--color-ink-muted);
		cursor: pointer;
		padding: var(--space-2);
		border-radius: var(--radius-sm);
	}

	.close:hover {
		color: var(--color-ink);
		background: var(--color-sunken);
	}

	.body {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: 0 0 var(--space-3);
	}

	.perms {
		margin: 0 0 var(--space-3);
		padding-left: var(--space-4);
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.perms li + li {
		margin-top: var(--space-2);
	}

	.status {
		font-size: var(--text-xs);
		margin-left: var(--space-2);
	}

	.status--ok {
		color: var(--color-ok);
	}

	.status--bad {
		color: var(--color-err);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.btn {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.btn:hover:not(:disabled) {
		border-color: var(--color-accent);
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.btn--primary {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}

	.keep-running {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		margin-top: var(--space-4);
		padding-top: var(--space-4);
		border-top: 1px solid var(--color-hairline);
	}

	.keep-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: 0;
	}
</style>
