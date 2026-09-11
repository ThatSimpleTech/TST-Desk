<script lang="ts">
	// Browser attach form (TD-3701). Shown only when the UI is not inside
	// Tauri and no ws+token target is known yet. The desktop window never
	// sees this — it still reads port.json through the host.
	import { connectRemote, remoteAttach } from '../connection-status.svelte.js';

	let wsUrl = $state('');
	let token = $state('');

	function onSubmit(event: SubmitEvent): void {
		event.preventDefault();
		connectRemote(wsUrl, token);
	}
</script>

{#if remoteAttach.needed}
	<div class="attach-overlay" role="dialog" aria-modal="true" aria-label="Connect to a daemon">
		<form class="attach" onsubmit={onSubmit}>
			<h1 class="title">Attach from this browser</h1>
			<p class="body">
				Same TST Desk client — no account, no hosted relay. Paste the Tailscale
				<code class="mono">ws://</code> address and the rotating token in
				<code class="mono">remote-token</code> (not <code class="mono">port.json</code>).
				Settings will copy both (TD-3603).
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
			{#if remoteAttach.error !== null}
				<p class="error" role="alert">{remoteAttach.error}</p>
			{/if}
			<div class="actions">
				<button class="btn btn--primary" type="submit">Connect</button>
			</div>
		</form>
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

	.body {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: 1.5;
	}

	.mono {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		user-select: all;
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

	.error {
		margin: 0;
		font-size: var(--text-xs);
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
		cursor: pointer;
	}

	.btn--primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--primary:hover {
		background: var(--color-accent-hover);
	}
</style>
