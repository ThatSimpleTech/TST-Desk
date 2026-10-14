<script lang="ts">
	// Phone / browser attach form (TD-3701, TD-5014). Shown only outside Tauri
	// while there is no working target. Pairing is the way in: scan the code
	// the computer shows, or type it. The TD-3701 URL + token fields stay under
	// "Use a token instead" for a computer that has not been paired with.
	import {
		connectRemote,
		pairFromTyped,
		remoteAttach,
		scanToPair,
		useSavedTarget,
	} from '../connection-status.svelte.js';
	import { attachStore } from '../attach-store';

	const durable = attachStore().durable;

	// A refused key comes back with its address kept (TD-5005, TD-5014).
	let wsUrl = $state(remoteAttach.lastUrl);
	let token = $state('');
	let address = $state(addressOf(remoteAttach.lastUrl));
	let code = $state('');
	let typing = $state(false);
	let useToken = $state(false);

	// No scanner (a browser tab, or an older shell): typing is the main way.
	let showTyped = $derived(typing || !remoteAttach.canScan);
	let title = $derived(
		remoteAttach.desktopName === null
			? 'Connect to your computer'
			: `Connect to ${remoteAttach.desktopName}`,
	);

	function addressOf(url: string): string {
		return url.replace(/^ws:\/\//, '');
	}

	$effect(() => {
		if (remoteAttach.lastUrl === '') return;
		if (wsUrl === '') wsUrl = remoteAttach.lastUrl;
		if (address === '') address = addressOf(remoteAttach.lastUrl);
	});

	function onPair(event: SubmitEvent): void {
		event.preventDefault();
		void pairFromTyped(address, code);
	}

	function onToken(event: SubmitEvent): void {
		event.preventDefault();
		connectRemote(wsUrl, token);
	}
</script>

{#if remoteAttach.needed}
	<div class="attach-overlay" role="dialog" aria-modal="true" aria-label="Connect to your computer">
		<div class="attach">
			<h1 class="title">{title}</h1>
			<p class="body">
				On the computer, open Settings, then Phones and devices, then Pair a phone.
			</p>
			{#if remoteAttach.storeNote !== null}
				<p class="note" role="status">{remoteAttach.storeNote}</p>
			{/if}
			{#if remoteAttach.error !== null}
				<p class="error" role="alert">{remoteAttach.error}</p>
			{/if}

			{#if remoteAttach.canScan}
				<button
					class="btn btn--primary btn--wide"
					type="button"
					disabled={remoteAttach.pairing}
					onclick={scanToPair}
					>{remoteAttach.pairing ? 'Pairing…' : 'Scan the code on your computer'}</button
				>
			{/if}
			{#if remoteAttach.fallback !== null && !remoteAttach.pairing}
				<button class="btn btn--wide" type="button" onclick={useSavedTarget}
					>Back to {remoteAttach.fallback}</button
				>
			{/if}

			{#if remoteAttach.canScan}
				<button
					class="disclosure"
					type="button"
					aria-expanded={typing}
					onclick={() => (typing = !typing)}>Type the code instead</button
				>
			{/if}
			{#if showTyped}
				<form class="fields" onsubmit={onPair}>
					<label class="field">
						<span class="field-label">Address</span>
						<input
							class="field-input"
							type="text"
							name="address"
							inputmode="url"
							placeholder="100.x.x.x:port"
							autocomplete="off"
							spellcheck="false"
							bind:value={address}
						/>
					</label>
					<label class="field">
						<span class="field-label">Code</span>
						<input
							class="field-input field-input--code"
							type="text"
							name="code"
							placeholder="ABCD-2345"
							autocomplete="off"
							autocapitalize="characters"
							spellcheck="false"
							bind:value={code}
						/>
					</label>
					<div class="actions">
						<button class="btn btn--primary" type="submit" disabled={remoteAttach.pairing}
							>{remoteAttach.pairing ? 'Pairing…' : 'Pair'}</button
						>
					</div>
				</form>
			{/if}

			{#if durable}
				<p class="hint">
					This phone remembers the computer, encrypted, and reconnects on launch. Forget it in
					Settings.
				</p>
			{/if}

			<button
				class="disclosure disclosure--small"
				type="button"
				aria-expanded={useToken}
				onclick={() => (useToken = !useToken)}>Use a token instead</button
			>
			{#if useToken}
				<form class="fields" onsubmit={onToken}>
					<p class="hint">
						Paste the Tailscale <code class="mono">ws://</code> address from Settings and the
						token in <code class="mono">remote-token</code> (not <code class="mono">port.json</code>).
					</p>
					<label class="field">
						<span class="field-label">WebSocket URL</span>
						<input
							class="field-input"
							type="text"
							name="ws"
							inputmode="url"
							placeholder="ws://100.x.x.x:port"
							autocomplete="off"
							spellcheck="false"
							bind:value={wsUrl}
						/>
					</label>
					<label class="field">
						<span class="field-label">Remote token</span>
						<input
							class="field-input"
							type="password"
							name="token"
							placeholder="contents of remote-token"
							autocomplete="off"
							bind:value={token}
						/>
					</label>
					<div class="actions">
						<button class="btn" type="submit">Connect</button>
					</div>
				</form>
			{/if}
		</div>
	</div>
{/if}

<style>
	.attach-overlay {
		position: fixed;
		inset: 0;
		background: color-mix(in srgb, var(--color-ground) 78%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: var(--z-modal);
		padding: var(--space-4);
	}

	.attach {
		width: min(34rem, calc(100vw - var(--space-8)));
		max-height: calc(100vh - var(--space-8));
		overflow-y: auto;
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-xl);
		box-shadow: var(--shadow-lg);
		padding: var(--space-6);
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.title {
		margin: 0;
		font-family: var(--font-display);
		font-size: var(--text-xl);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-ink);
	}

	.body,
	.hint,
	.note {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: 1.5;
	}

	.hint {
		font-size: var(--text-xs);
	}

	.note {
		color: var(--color-warn);
	}

	.mono {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		user-select: all;
	}

	.fields {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.field-label {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
	}

	.field-input {
		font-size: var(--text-sm);
		font-family: var(--font-mono);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
		color: var(--color-ink);
	}

	.field-input--code {
		font-size: var(--text-lg);
		letter-spacing: 0.08em;
		text-transform: uppercase;
	}

	.error {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-err);
	}

	.actions {
		display: flex;
		justify-content: flex-end;
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-md);
		border: var(--border-width) solid var(--color-hairline);
		background: transparent;
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.6;
		cursor: default;
	}

	.btn--wide {
		width: 100%;
		padding: var(--space-3);
	}

	.btn--primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--primary:hover:not(:disabled) {
		background: var(--color-accent-hover);
	}

	.disclosure {
		align-self: flex-start;
		background: transparent;
		border: 0;
		padding: 0;
		font-size: var(--text-sm);
		color: var(--color-accent);
		cursor: pointer;
	}

	.disclosure--small {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}
</style>
