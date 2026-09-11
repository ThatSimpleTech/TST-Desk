<script lang="ts">
	// Computer-use permission / integrity explanation (TD-3302, TD-3303).
	// Same panel for first-run and Settings reopen. macOS is TCC; Windows
	// is the missing grant dialog plus UIPI and the secure desktop; Linux
	// is X11 no-gate honesty plus named Wayland / XTEST / display limits.
	// The macOS branch lives in CuPermissionsMacos.svelte (TD-4823): it
	// also shows stale grants and the reset action.
	import {
		cuPermissions,
		closeCuPermissions,
		openCuPermissions,
		retryCuPermissions,
	} from '../cu-permissions.svelte.js';
	import CuPermissionsMacos from './CuPermissionsMacos.svelte';
	import Icon from './Icon.svelte';

	interface Props {
		variant?: 'dialog' | 'settings';
	}

	let { variant = 'dialog' }: Props = $props();

	const isWindows = $derived(cuPermissions.platform === 'windows');
	const isLinux = $derived(cuPermissions.platform === 'linux');

	const settingsHint = $derived(
		isWindows
			? 'Reopens the UIPI and secure-desktop explanation.'
			: isLinux
				? 'Reopens the X11 / Wayland computer-use explanation.'
				: cuPermissions.platform === 'macos'
					? 'Reopens the Screen Recording and Accessibility explanation.'
					: 'Reopens the computer-use explanation for this OS.',
	);
</script>

{#if variant === 'settings'}
	<div class="keep-running">
		<p class="keep-title">Computer use permissions</p>
		<button class="btn" type="button" onclick={() => openCuPermissions()}
			>Show explanation</button
		>
	</div>
	<p class="hint">{settingsHint}</p>
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
			{#if isLinux}
				<p class="body">
					Linux will not prompt for a computer-use permission — there is nothing to click and
					nothing to grant. Native X11 can capture and inject input. A Wayland session cannot.
				</p>
				<ul class="perms">
					<li>
						<strong>No grant dialog</strong>
						— {cuPermissions.noGate ||
							'X11 has no Screen Recording or Accessibility analog.'}
					</li>
					<li>
						<strong>Session</strong>
						— {cuPermissions.sessionType || 'unknown'}
						<span
							class="status"
							class:status--ok={cuPermissions.sessionType === 'x11' && cuPermissions.granted}
							class:status--bad={cuPermissions.sessionType !== 'x11' || !cuPermissions.granted}
						>
							{cuPermissions.granted ? 'usable' : 'not usable'}
						</span>
					</li>
					{#if cuPermissions.waylandApplies || cuPermissions.wayland}
						<li>
							<strong>Wayland</strong>
							— {cuPermissions.wayland ||
								'This session cannot be captured or driven. Use X11.'}
							<span class="status status--bad">unsupported</span>
						</li>
					{/if}
					{#if cuPermissions.xtestApplies || cuPermissions.xtest}
						<li>
							<strong>XTEST</strong>
							— {cuPermissions.xtest ||
								'The XTEST extension is missing; mouse and keyboard synthesis will not work.'}
							<span class="status status--bad">missing</span>
						</li>
					{/if}
					{#if cuPermissions.noDisplayApplies || cuPermissions.noDisplay}
						<li>
							<strong>Display</strong>
							— {cuPermissions.noDisplay ||
								'No X11 display is open. Capture and input are unavailable.'}
							<span class="status status--bad">unavailable</span>
						</li>
					{/if}
				</ul>
				<div class="actions">
					<button
						class="btn btn--primary"
						type="button"
						disabled={cuPermissions.probing}
						onclick={() => retryCuPermissions()}>Retry</button
					>
				</div>
				{#if cuPermissions.probing}
					<p class="hint" aria-live="polite">Checking session…</p>
				{:else if cuPermissions.granted}
					<p class="hint" aria-live="polite">
						There is no permission to grant. This X11 session can capture and inject input.
					</p>
				{:else}
					<p class="hint" aria-live="polite">
						Switch to a native X11 session. An XWayland DISPLAY on Wayland is not enough.
					</p>
				{/if}
			{:else if isWindows}
				<p class="body">
					Windows will not prompt for a computer-use permission — there is nothing to click and
					nothing to grant. Capture and input are allowed. Two conditions still fail silently if
					you refuse the boundary they enforce.
				</p>
				<ul class="perms">
					<li>
						<strong>No grant dialog</strong>
						— {cuPermissions.noGate ||
							'Windows has no Screen Recording or Accessibility analog.'}
					</li>
					<li>
						<strong>Elevated windows (UIPI)</strong>
						— {cuPermissions.uipi ||
							'Clicks and keys aimed at a Run as administrator window are discarded.'}
						<span
							class="status"
							class:status--ok={!cuPermissions.uipiApplies}
							class:status--bad={cuPermissions.uipiApplies}
						>
							{cuPermissions.uipiApplies ? 'live for this process' : 'not live (elevated)'}
						</span>
					</li>
					<li>
						<strong>Secure desktop</strong>
						— {cuPermissions.secureDesktop ||
							'UAC, the lock screen, and Ctrl+Alt+Del cannot be captured or driven. No workaround.'}
						<span class="status status--bad">always a boundary</span>
					</li>
				</ul>
				<div class="actions">
					<button
						class="btn btn--primary"
						type="button"
						disabled={cuPermissions.probing}
						onclick={() => retryCuPermissions()}>Retry</button
					>
				</div>
				{#if cuPermissions.probing}
					<p class="hint" aria-live="polite">Checking integrity…</p>
				{:else}
					<p class="hint" aria-live="polite">
						There is no permission to grant. Drive unelevated windows, and never the UAC prompt.
					</p>
				{/if}
			{:else}
				<CuPermissionsMacos />
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
		font-size: var(--text-lg);
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
