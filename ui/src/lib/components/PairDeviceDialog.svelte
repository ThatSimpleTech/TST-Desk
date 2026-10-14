<script lang="ts">
	// "Pair a phone" (TD-5014). Shows the daemon's code as a QR, the typed
	// form, the address to type it with, and a live countdown. The QR is the
	// daemon's SVG as an <img> data URL: an image cannot run script, and
	// nothing here goes through innerHTML or {@html}.
	import { onDestroy } from 'svelte';
	import { cancelPairDialog, pairDialog, requestPairingCode } from '../pair-device.svelte.js';
	import { formatCountdown } from '../pairing';

	let now = $state(Date.now());

	$effect(() => {
		if (!pairDialog.open) return;
		now = Date.now();
		const id = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(id);
	});

	// Leaving Settings mid-pairing must not leave a code live for ten minutes.
	onDestroy(() => {
		if (pairDialog.open) cancelPairDialog();
	});

	let offer = $derived(pairDialog.offer);
	let remaining = $derived(offer === null ? 0 : Date.parse(offer.expires_at) - now);
	let expired = $derived(offer !== null && !(remaining > 0));
	let qrSrc = $derived(offer === null ? '' : `data:image/svg+xml;base64,${toBase64(offer.qr_svg)}`);
	let address = $derived(
		offer === null ? '' : offer.host.includes(':') ? `[${offer.host}]:${offer.port}` : `${offer.host}:${offer.port}`,
	);

	// btoa takes Latin-1 only; go through UTF-8 so any SVG text encodes.
	function toBase64(text: string): string {
		let binary = '';
		for (const byte of new TextEncoder().encode(text)) binary += String.fromCharCode(byte);
		return btoa(binary);
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.stopPropagation();
		cancelPairDialog();
	}
</script>

{#if pairDialog.open}
	<div
		class="overlay"
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label="Pair a phone"
		onkeydown={onKeydown}
	>
		<div class="card">
			<h2 class="title">Pair a phone</h2>
			{#if pairDialog.paired !== null}
				<p class="done" role="status">Paired with {pairDialog.paired.name}</p>
			{:else if pairDialog.error !== null}
				<p class="error" role="alert">{pairDialog.error}</p>
				<div class="actions">
					<button class="btn" type="button" onclick={cancelPairDialog}>Close</button>
					<button class="btn btn--primary" type="button" onclick={requestPairingCode}
						>Try again</button
					>
				</div>
			{:else if offer === null}
				<p class="body" role="status">Getting a code…</p>
				<div class="actions">
					<button class="btn" type="button" onclick={cancelPairDialog}>Cancel</button>
				</div>
			{:else if expired}
				<p class="body" role="status">This code expired</p>
				<div class="actions">
					<button class="btn" type="button" onclick={cancelPairDialog}>Cancel</button>
					<button class="btn btn--primary" type="button" onclick={requestPairingCode}
						>New code</button
					>
				</div>
			{:else}
				<p class="body">
					In TST Desk on the phone, tap <strong>Scan the code on your computer</strong>.
				</p>
				<img class="qr" src={qrSrc} alt="Pairing QR code" width="200" height="200" />
				<p class="code" aria-label="Pairing code">{offer.display_code}</p>
				<p class="body">
					Or tap <strong>Type the code instead</strong> and enter
					<code class="mono">{address}</code> and the code above.
				</p>
				<p class="countdown">Expires in {formatCountdown(remaining)}</p>
				<div class="actions">
					<button class="btn" type="button" onclick={cancelPairDialog}>Cancel</button>
				</div>
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

	.card {
		width: min(24rem, 92vw);
		max-height: 90vh;
		overflow-y: auto;
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-lg);
		padding: var(--space-5);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-3);
		text-align: center;
	}

	.title {
		margin: 0;
		font-size: var(--text-lg);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.body,
	.countdown {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: 1.5;
	}

	.countdown {
		font-variant-numeric: tabular-nums;
	}

	.qr {
		display: block;
		background: var(--color-lifted);
		border-radius: var(--radius-md);
	}

	.code {
		margin: 0;
		font-family: var(--font-mono);
		font-size: var(--text-3xl);
		font-weight: var(--weight-semibold);
		letter-spacing: 0.08em;
		color: var(--color-ink);
		user-select: all;
	}

	.mono {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		user-select: all;
	}

	.done {
		margin: var(--space-4) 0;
		font-size: var(--text-base);
		font-weight: var(--weight-medium);
		color: var(--color-ok);
	}

	.error {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-err);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		justify-content: center;
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-md);
		border: 1px solid var(--color-hairline);
		background: transparent;
		color: var(--color-ink);
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
