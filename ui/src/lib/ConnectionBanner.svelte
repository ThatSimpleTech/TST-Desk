<script lang="ts">
	import { ws, daemon } from './connection-status';

	// Presentational only: both values come straight from the store, which
	// reads them from the host event and the real socket (AGENTS §6 — the UI
	// never derives truth it wasn't given).
	let label = $derived(bannerLabel());
	let tone = $derived(bannerTone());

	function bannerLabel(): string {
		if (ws.state === 'connecting' || ws.state === 'reconnecting') return 'Connecting…';
		if (ws.state === 'disconnected') {
			if (daemon.state === 'crashed') return 'Daemon crashed — reconnecting';
			if (daemon.state === 'stopping' || daemon.state === 'stopped') return 'Daemon stopped';
			return 'Not connected';
		}
		if (ws.state === 'connected') return 'Connected';
		return 'Stopped';
	}

	function bannerTone(): string {
		if (ws.state === 'connected') return 'success';
		if (ws.state === 'connecting' || ws.state === 'reconnecting') return 'info';
		return 'warning';
	}
</script>

<button
	class="banner banner--{tone}"
	type="button"
	title={daemon.state === 'crashed' && daemon.restart > 0
		? `restarted ${daemon.restart}×`
		: undefined}
>
	<span class="dot" aria-hidden="true"></span>
	<span>{label}</span>
</button>

<style>
	.banner {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-text);
		background: var(--color-bg-subtle);
		border: 1px solid var(--color-border);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
	}

	.dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-text-muted);
	}

	.banner--success .dot {
		background: var(--color-success);
	}

	.banner--warning .dot {
		background: var(--color-warning);
	}

	.banner--info .dot {
		background: var(--color-info);
	}
</style>