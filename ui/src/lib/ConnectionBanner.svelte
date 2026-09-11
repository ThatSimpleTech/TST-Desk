<script lang="ts">
	import { ws, daemon } from './connection-status.svelte.js';
	import { bannerLabel, bannerTone } from './connection-banner';

	// Presentational only: both values come straight from the store, which
	// reads them from the host event and the real socket (AGENTS §6 — the UI
	// never derives truth it wasn't given). Copy lives in connection-banner.ts.
	let label = $derived(bannerLabel(ws.state, daemon.state));
	let tone = $derived(bannerTone(ws.state, daemon.state));

	// Connected is the normal state and shows nothing at all: a healthy
	// socket is not news, and a permanent green dot only teaches the eye to
	// stop looking. Every other state reads out loud as a labelled pill in
	// this same spot, so the header changes exactly when something is wrong.
	// The live region stays mounted either way, so assistive tech hears the
	// transition in both directions.
	let quiet = $derived(ws.state === 'connected');

	let hint = $derived(
		daemon.state === 'crashed' && daemon.restart > 0
			? `${label} · restarted ${daemon.restart}×`
			: label === 'Couldn’t start the daemon'
				? 'The daemon started but the window never attached. Quit and reopen the app.'
				: label,
	);
</script>

<span class="status" class:visually-hidden={quiet} role="status">
	{#if quiet}
		{label}
	{:else}
		<span class="banner banner--{tone}" title={hint}>
			<span class="dot" aria-hidden="true"></span>
			<span class="label">{label}</span>
		</span>
	{/if}
</span>

<style>
	.status {
		display: inline-flex;
		align-items: center;
	}

	/* Out of the header's flex flow while connected, so the wordmark and the
	   title bar close up with no phantom gap; still in the accessibility
	   tree, so the state change is announced. */
	.visually-hidden {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		border: 0;
		overflow: hidden;
		clip: rect(0 0 0 0);
		clip-path: inset(50%);
		white-space: nowrap;
	}

	.banner {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: default;
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-ink-muted);
		flex-shrink: 0;
	}

	.banner--warning .dot {
		background: var(--color-warn);
	}

	.banner--info .dot {
		background: var(--color-accent);
	}

	.label {
		white-space: nowrap;
	}
</style>
