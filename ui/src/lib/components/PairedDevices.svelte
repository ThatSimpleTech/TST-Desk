<script lang="ts">
	// Paired phones (TD-5014). Presentational over settings.remoteDevices,
	// which only setup_state fills. Remove asks first, because the phone has
	// to pair again afterwards. A phone viewing this list cannot change it:
	// the daemon only takes pair/remove from the computer itself. The paired
	// date sits next to "last seen" so two phones with one name differ.
	import { onMount } from 'svelte';
	import { settings } from '../settings.svelte.js';
	import { refreshRemoteDevices, removeRemoteDevice } from '../pair-device.svelte.js';
	import { pairedWords, relativeWords } from '../pairing';
	import type { RemoteDeviceSummary } from '../protocol';

	let { canManage }: { canManage: boolean } = $props();

	let confirming = $state<string | null>(null);
	let now = $state(Date.now());

	// The daemon pushes presence from here on; this is the first read.
	onMount(refreshRemoteDevices);

	// "5 minutes ago" goes stale while Settings stays open.
	$effect(() => {
		const id = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(id);
	});

	function seen(device: RemoteDeviceSummary): string {
		if (device.connected) return 'Connected now';
		if (device.last_seen === null) return 'Not connected yet';
		return `Last seen ${relativeWords(device.last_seen, now)}`;
	}

	function remove(device: RemoteDeviceSummary): void {
		removeRemoteDevice(device.id);
		confirming = null;
	}
</script>

{#if settings.remoteDevices.length === 0}
	<p class="empty">No phones paired yet.</p>
{:else}
	<ul class="devices" aria-label="Paired phones">
		{#each settings.remoteDevices as device (device.id)}
			<li class="device">
				<span
					class="dot"
					class:dot--on={device.connected}
					role="img"
					aria-label={device.connected ? 'Connected' : 'Not connected'}
				></span>
				<span class="who">
					<span class="name">{device.name}</span>
					{#if device.kind === 'machine'}
						<!-- TD-5015: another computer that paired with this one. -->
						<span class="seen">A computer, not a phone</span>
					{/if}
					<span class="seen">{seen(device)}</span>
					<span class="seen">{pairedWords(device.paired_at, now)}</span>
				</span>
				{#if canManage}
					{#if confirming === device.id}
						<span class="confirm">
							<span class="confirm-text">It will need to pair again.</span>
							<button class="choice choice--danger" type="button" onclick={() => remove(device)}
								>Remove</button
							>
							<button class="choice" type="button" onclick={() => (confirming = null)}>Keep</button>
						</span>
					{:else}
						<button
							class="choice"
							type="button"
							aria-label={`Remove ${device.name}`}
							onclick={() => (confirming = device.id)}>Remove</button
						>
					{/if}
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.empty {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}

	.devices {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.device {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-2) var(--space-3);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
	}

	.dot {
		flex: 0 0 auto;
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-ink-muted);
		opacity: 0.5;
	}

	.dot--on {
		background: var(--color-ok);
		opacity: 1;
	}

	.who {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-direction: column;
	}

	.name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.seen {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.confirm {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
	}

	.confirm-text {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.choice {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.choice--danger {
		color: var(--color-err);
		border-color: var(--color-err);
	}
</style>
