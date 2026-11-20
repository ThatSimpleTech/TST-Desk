<script lang="ts">
	// "What paired phones may change" (TD-5279). On the computer only: the
	// daemon takes the switch from nowhere else, and this block is not
	// rendered on a phone. Turning one on goes through its disclosure first;
	// turning it off is one press, because that only narrows.
	import { settings, setPhonePermission } from '../settings.svelte.js';
	import {
		ONE_HOUR_SECONDS,
		PHONE_PERMISSIONS,
		isGranted,
		stateWords,
		type PhonePermissionCopy,
	} from '../phone-permissions';

	let asking = $state<PhonePermissionCopy | null>(null);
	let now = $state(Date.now());

	// A timed grant runs out while Settings stays open.
	$effect(() => {
		const id = setInterval(() => (now = Date.now()), 15_000);
		return () => clearInterval(id);
	});

	function turnOn(expiresInSeconds: number | null): void {
		if (asking === null) return;
		setPhonePermission(asking.capability, true, expiresInSeconds);
		asking = null;
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.stopPropagation();
		asking = null;
	}
</script>

<div class="block">
	<p class="heading">What paired phones may change</p>
	<p class="note">
		Each of these is off until you turn it on here. A phone cannot turn one on.
	</p>
	<ul class="list" aria-label="Phone permissions">
		{#each PHONE_PERMISSIONS as permission (permission.capability)}
			{@const on = isGranted(settings.phonePermissions, permission.capability, now)}
			<li class="row">
				<span class="what">
					<span class="name">{permission.title}</span>
					<span class="state" class:state--on={on}
						>{stateWords(settings.phonePermissions, permission.capability, now)}</span
					>
				</span>
				{#if on}
					<button
						class="choice"
						type="button"
						aria-label={`Turn off: ${permission.title}`}
						onclick={() => setPhonePermission(permission.capability, false, null)}
						>Turn off</button
					>
				{:else}
					<button
						class="choice"
						type="button"
						aria-label={`Turn on: ${permission.title}`}
						onclick={() => (asking = permission)}>Turn on…</button
					>
				{/if}
			</li>
		{/each}
	</ul>
</div>

{#if asking !== null}
	<div
		class="overlay"
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label={asking.title}
		onkeydown={onKeydown}
	>
		<div class="card">
			<h2 class="title">{asking.title}</h2>
			<p class="body">{asking.lead}</p>
			<ul class="points">
				{#each asking.points as point (point)}
					<li>{point}</li>
				{/each}
			</ul>
			<p class="body">{asking.advice}</p>
			<div class="actions">
				<!-- svelte-ignore a11y_autofocus -->
				<button class="btn btn--primary" type="button" autofocus onclick={() => (asking = null)}
					>Keep off</button
				>
				<button class="btn" type="button" onclick={() => turnOn(ONE_HOUR_SECONDS)}
					>Turn on for 1 hour</button
				>
				<button class="btn btn--danger" type="button" onclick={() => turnOn(null)}
					>Turn on until I turn it off</button
				>
			</div>
		</div>
	</div>
{/if}

<style>
	.block {
		margin-top: var(--space-5);
	}

	.heading {
		margin: 0;
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

	.note {
		margin: var(--space-1) 0 0;
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.list {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.row {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-2) var(--space-3);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
	}

	.what {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-direction: column;
	}

	.name {
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.state {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.state--on {
		color: var(--color-warn);
		font-weight: var(--weight-medium);
	}

	.choice {
		flex: 0 0 auto;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.overlay {
		position: fixed;
		inset: 0;
		z-index: var(--z-modal);
		display: grid;
		place-items: center;
		background: rgb(0 0 0 / 0.28);
	}

	.card {
		width: min(30rem, 92vw);
		max-height: 90vh;
		overflow-y: auto;
		background: var(--color-lifted);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-lg);
		box-shadow: var(--shadow-lg);
		padding: var(--space-5);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.title {
		margin: 0;
		font-size: var(--text-lg);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.body,
	.points {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink);
		line-height: 1.5;
	}

	.points {
		padding-left: var(--space-5);
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		justify-content: flex-end;
		margin-top: var(--space-2);
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

	.btn--danger {
		color: var(--color-err);
		border-color: var(--color-err);
	}
</style>
