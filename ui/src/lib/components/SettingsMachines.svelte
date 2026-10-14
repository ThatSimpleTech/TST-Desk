<script lang="ts">
	// Settings → Computers (TD-5015): the other computers this one reaches,
	// and the form that pairs one more. Presentational over machines.svelte.ts;
	// validation and copy live in machines.ts.
	//
	// Only the hub itself may add or remove a computer, so a phone gets the
	// list and a line saying where to change it. "Try now" is the hub's too.
	import { onMount } from 'svelte';
	import { pairedWords } from '../device-words';
	import { hub, hubKnowsMachines } from '../hub.svelte.js';
	import {
		STATUS_WORDS,
		lastSeenWords,
		machineLabel,
		readOnlyCopy,
		removeConfirmCopy,
		statusTone,
		type AddMachineInput
	} from '../machines';
	import {
		addMachine,
		askRemoveMachine,
		cancelRemoveMachine,
		clearAddFeedback,
		machines,
		reconnectMachine,
		refreshMachines,
		removeMachine
	} from '../machines.svelte.js';
	import { isTauri } from '../open-file';
	import { remoteAttach } from '../remote-attach.svelte.js';
	import type { MachineSummary } from '../protocol';

	const onComputer = isTauri();
	let mode = $state<'link' | 'typed'>('link');
	let link = $state('');
	let address = $state('');
	let code = $state('');
	let now = $state(Date.now());

	let supported = $derived(hubKnowsMachines());
	let hubName = $derived(hub.desktopName ?? remoteAttach.desktopName ?? 'the computer');

	onMount(() => {
		refreshMachines();
		return () => clearAddFeedback();
	});

	// "2 hours ago" goes stale while Settings stays open.
	$effect(() => {
		const id = setInterval(() => (now = Date.now()), 30_000);
		return () => clearInterval(id);
	});

	function seen(machine: MachineSummary): string {
		if (machine.status === 'connected') return STATUS_WORDS.connected;
		const last = lastSeenWords(machine, now);
		return last === null ? STATUS_WORDS[machine.status] : `${STATUS_WORDS[machine.status]} · ${last}`;
	}

	function submit(event: SubmitEvent): void {
		event.preventDefault();
		const input: AddMachineInput = mode === 'link' ? { link } : { address, code };
		if (addMachine(input)) {
			// The code is single-use; never keep it on screen once it is sent.
			code = '';
			link = '';
		}
	}

	function pickMode(next: 'link' | 'typed'): void {
		mode = next;
		clearAddFeedback();
	}
</script>

{#if !supported}
	<p class="hint">
		{onComputer
			? 'This version of TST Desk cannot pair other computers yet.'
			: `${hubName} needs a newer TST Desk to show other computers.`}
	</p>
{:else}
	<p class="hint">
		Sessions on a paired computer show up in the session list under its name. It reaches that
		computer over Tailscale.
	</p>

	{#if machines.list.length === 0}
		<p class="empty">{machines.loaded ? 'No other computers yet.' : 'Loading…'}</p>
	{:else}
		<ul class="machines" aria-label="Computers">
			{#each machines.list as machine (machine.id)}
				<li class="machine">
					<span
						class="dot dot-{statusTone(machine.status)}"
						role="img"
						aria-label={STATUS_WORDS[machine.status]}
					></span>
					<span class="who">
						<span class="name">{machineLabel(machine, machines.list)}</span>
						<span class="seen">{seen(machine)}</span>
						<span class="seen">{pairedWords(machine.paired_at, now)}</span>
						{#if machine.detail}
							<span class="seen">{machine.detail}</span>
						{/if}
					</span>
					{#if onComputer}
						{#if machines.confirmRemoveFor === machine.id}
							<span class="confirm" role="alertdialog" aria-label={`Remove ${machine.name}`}>
								<span class="confirm-text">{removeConfirmCopy(machine)}</span>
								<span class="confirm-btns">
									<button
										class="choice choice--danger"
										type="button"
										onclick={() => removeMachine(machine.id)}>Remove</button
									>
									<button class="choice" type="button" onclick={cancelRemoveMachine}>Keep</button>
								</span>
							</span>
						{:else}
							{#if machine.status !== 'connected'}
								<button
									class="choice"
									type="button"
									aria-label={`Try reaching ${machine.name} now`}
									onclick={() => reconnectMachine(machine.id)}>Try now</button
								>
							{/if}
							<button
								class="choice"
								type="button"
								aria-label={`Remove ${machine.name}`}
								onclick={() => askRemoveMachine(machine.id)}>Remove</button
							>
						{/if}
					{/if}
				</li>
			{/each}
		</ul>
	{/if}

	{#if onComputer}
		<h2 class="sub">Add a computer</h2>
		<p class="hint">
			On the other computer, open Settings → Phones and devices → Pair a phone, then Copy link.
			Or type its address and the code it shows.
		</p>
		<div class="modes" role="radiogroup" aria-label="How to add">
			<button
				class="choice"
				class:choice--active={mode === 'link'}
				type="button"
				role="radio"
				aria-checked={mode === 'link'}
				onclick={() => pickMode('link')}>Paste link</button
			>
			<button
				class="choice"
				class:choice--active={mode === 'typed'}
				type="button"
				role="radio"
				aria-checked={mode === 'typed'}
				onclick={() => pickMode('typed')}>Address and code</button
			>
		</div>
		<form class="add" onsubmit={submit}>
			{#if mode === 'link'}
				<label class="field">
					<span class="label">Pairing link</span>
					<input
						class="input"
						type="text"
						autocomplete="off"
						spellcheck="false"
						placeholder="tstdesk://pair?…"
						bind:value={link}
						disabled={machines.adding}
					/>
				</label>
			{:else}
				<label class="field">
					<span class="label">Address</span>
					<input
						class="input"
						type="text"
						autocomplete="off"
						spellcheck="false"
						placeholder="100.x.y.z:39079"
						bind:value={address}
						disabled={machines.adding}
					/>
				</label>
				<label class="field">
					<span class="label">Code</span>
					<input
						class="input"
						type="text"
						inputmode="numeric"
						autocomplete="one-time-code"
						spellcheck="false"
						bind:value={code}
						disabled={machines.adding}
					/>
				</label>
			{/if}
			<div class="actions">
				<button class="choice choice--primary" type="submit" disabled={machines.adding}>
					{machines.adding ? 'Adding…' : 'Add'}
				</button>
			</div>
		</form>
		{#if machines.addError !== null}
			<p class="error" role="alert">{machines.addError}</p>
		{:else if machines.added !== null}
			<p class="ok" role="status">Added {machines.added}. Its sessions show up in the list.</p>
		{/if}
	{:else}
		<p class="hint">{readOnlyCopy(hubName)}</p>
	{/if}
{/if}

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-2) 0 0;
	}

	.empty {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}

	.sub {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: var(--space-5) 0 0;
	}

	.machines {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.machine {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
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
	}
	.dot-ok {
		background: var(--color-ok);
	}
	.dot-info {
		background: var(--color-accent);
	}
	.dot-warn {
		background: var(--color-warn);
	}
	.dot-muted {
		background: var(--color-ink-muted);
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
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		flex-basis: 100%;
	}

	.confirm-text {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.confirm-btns {
		display: inline-flex;
		gap: var(--space-2);
	}

	.modes {
		display: inline-flex;
		gap: var(--space-2);
		margin: var(--space-3) 0 0;
	}

	.add {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		margin: var(--space-3) 0 0;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.label {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.input {
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-sunken);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-2);
	}

	.actions {
		display: flex;
		justify-content: flex-end;
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

	.choice:disabled {
		color: var(--color-ink-muted);
		cursor: default;
	}

	.choice--active {
		color: var(--color-ink);
		border-color: var(--color-accent);
	}

	.choice--primary {
		color: var(--color-ink);
		border-color: var(--color-accent);
	}

	.choice--danger {
		color: var(--color-err);
		border-color: var(--color-err);
	}

	.error {
		font-size: var(--text-sm);
		color: var(--color-err);
		margin: var(--space-2) 0 0;
	}

	.ok {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-2) 0 0;
	}
</style>
