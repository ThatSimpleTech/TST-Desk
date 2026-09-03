<script lang="ts">
	// macOS branch of the computer-use permissions pane (TD-4823).
	// TCC pins each grant to the app's code signature, so a grant made
	// for an older build stays ON in System Settings and still fails.
	// The host reports that as stale; the only repair is a reset, which
	// is a user action here and never a tool.
	import {
		cuPermissions,
		openSystemSettings,
		resetCuGrants,
		retryCuPermissions,
	} from '../cu-permissions.svelte.js';

	const screenOk = $derived(cuPermissions.screenRecording);
	const accessOk = $derived(cuPermissions.accessibility);
	const screenStale = $derived(!screenOk && cuPermissions.staleScreenRecording);
	const accessStale = $derived(!accessOk && cuPermissions.staleAccessibility);
	const anyStale = $derived(screenStale || accessStale);
	const adhoc = $derived(cuPermissions.signing === 'adhoc');
	const offerReset = $derived(
		cuPermissions.resetSupported && (anyStale || (adhoc && !cuPermissions.granted)),
	);
	const hostMissing = $derived(
		cuPermissions.actuationPath !== '' && cuPermissions.actuationPath !== 'host',
	);
	const fixText = $derived(
		!screenOk
			? cuPermissions.fixScreenRecording
			: !accessOk
				? cuPermissions.fixAccessibility
				: '',
	);

	function status(ok: boolean, stale: boolean): 'granted' | 'stale' | 'denied' {
		return ok ? 'granted' : stale ? 'stale' : 'denied';
	}
</script>

<p class="body">
	Desktop computer use needs two macOS permissions, granted to this app — not the sidecar. macOS
	ties each grant to the exact app build: a grant made for an older build still shows ON in System
	Settings but no longer works.
	{#if cuPermissions.signing === 'identity'}
		This build is signed with a stable identity, so grants survive rebuilds.
	{/if}
	Enable TST Desk in System Settings, then fully Quit (Cmd+Q) and reopen. Clicking Allow on the
	prompt is not enough.
</p>
{#if hostMissing}
	<p class="warn">
		The TST Desk host actuator is not running; capture and input would be attributed to a helper
		process, not TST Desk. Start the packaged app and retry.
	</p>
{/if}
{#if cuPermissions.unbundledDevBinary}
	<p class="warn">
		This is an unbundled development binary. macOS lists it as a lowercase “tst-desk” row keyed by
		path; those rows never apply to the installed app and can be removed with “−”.
	</p>
{/if}
{#if adhoc}
	<p class="warn">
		This build is ad-hoc signed, so every rebuild invalidates its grants. Install with
		<code>shell/scripts/install-macos.sh</code> to sign with a stable identity.
	</p>
{/if}
<ul class="perms">
	<li>
		<strong>Screen Recording</strong>
		— capture the desktop (screenshots).
		<span
			class="status"
			class:status--ok={screenOk}
			class:status--stale={screenStale}
			class:status--bad={!screenOk && !screenStale}
		>
			{status(screenOk, screenStale)}
		</span>
		{#if screenStale}
			<span class="detail">Settings shows ON, but that grant belongs to an older build.</span>
		{/if}
	</li>
	<li>
		<strong>Accessibility</strong>
		— move the pointer, click, type, and scroll.
		<span
			class="status"
			class:status--ok={accessOk}
			class:status--stale={accessStale}
			class:status--bad={!accessOk && !accessStale}
		>
			{status(accessOk, accessStale)}
		</span>
		{#if accessStale}
			<span class="detail">Settings shows ON, but that grant belongs to an older build.</span>
		{/if}
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
		class="btn"
		class:btn--primary={!offerReset}
		type="button"
		disabled={cuPermissions.probing}
		onclick={() => retryCuPermissions()}>Retry</button
	>
	{#if offerReset}
		<button
			class="btn btn--primary"
			type="button"
			disabled={cuPermissions.probing}
			onclick={() => resetCuGrants()}>Reset grants &amp; re-request</button
		>
	{/if}
</div>
{#if cuPermissions.resetError !== ''}
	<p class="warn">
		TST Desk could not reset the grants itself ({cuPermissions.resetError}). Run these in
		Terminal, then relaunch TST Desk:
	</p>
	<pre class="cmd">tccutil reset ScreenCapture com.thatsimpletech.tstdesk
tccutil reset Accessibility com.thatsimpletech.tstdesk</pre>
{/if}
{#if cuPermissions.probing}
	<p class="hint" aria-live="polite">Checking permissions…</p>
{:else if cuPermissions.granted}
	<p class="hint" aria-live="polite">Both permissions are granted. You can retry the turn.</p>
{:else if offerReset}
	<p class="hint" aria-live="polite">
		Reset removes this app's Screen Recording and Accessibility rows; macOS will prompt again.
		Allow, then Quit (Cmd+Q) and reopen once.
	</p>
{:else if fixText !== ''}
	<p class="hint" aria-live="polite">{fixText}</p>
{/if}

<style>
	.body {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: 0 0 var(--space-3);
	}

	.warn {
		font-size: var(--text-sm);
		color: var(--color-warn, var(--color-err));
		margin: 0 0 var(--space-3);
	}

	.warn code {
		font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
		font-size: var(--text-xs);
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

	.status--stale {
		color: var(--color-warn, var(--color-err));
	}

	.status--bad {
		color: var(--color-err);
	}

	.detail {
		display: block;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		margin-top: var(--space-1);
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

	.cmd {
		font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
		font-size: var(--text-xs);
		color: var(--color-ink);
		background: var(--color-sunken);
		border-radius: var(--radius-sm);
		padding: var(--space-2) var(--space-3);
		margin: 0 0 var(--space-3);
		overflow-x: auto;
		user-select: all;
	}
</style>
