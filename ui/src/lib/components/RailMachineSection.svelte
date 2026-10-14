<script lang="ts">
	// One computer's block in the rail (TD-5015). SessionRail draws these
	// only once another computer is paired; with none it keeps its old list.
	//
	// The header names the computer and, for another computer, its status:
	// a dot, "Not reachable · last seen 2 h ago", "Try now" (desktop only:
	// the reconnect verb is the hub's own) and "+ New here", which starts a
	// session in one of the workspaces that computer's rows already name.
	// Rows group by recency exactly as the single list does.
	import EmptyState from './EmptyState.svelte';
	import Icon from './Icon.svelte';
	import RailSessionRow from './RailSessionRow.svelte';
	import { machineOf } from '../bridged-id';
	import {
		STATUS_WORDS,
		firstSessionCopy,
		isOffline,
		knownWorkspaces,
		statusLine,
		statusTone,
		type ComputerSection
	} from '../machines';
	import { reconnectMachine } from '../machines.svelte.js';
	import { isTauri } from '../open-file';
	import { groupRowsByRecency } from '../rail';
	import { workspaceName } from '../session-status.svelte.js';
	import { newSessionFrom, sessions, type SessionRow } from '../sessions.svelte.js';

	let {
		section,
		activeId,
		emptyCopy,
		onselect
	}: {
		section: ComputerSection<SessionRow>;
		activeId: string | null;
		emptyCopy: string;
		onselect: (sessionId: string) => void;
	} = $props();

	let remote = $derived(section.id !== 'local');
	let machine = $derived(section.machine);
	let offline = $derived(isOffline(machine));
	let groups = $derived(groupRowsByRecency(section.rows));
	let note = $derived(machine === null ? null : statusLine(machine));
	// "+ New here" offers every workspace this computer's rows name, not just
	// the rows the filter or shelf leaves visible.
	let ownRows = $derived(remote ? sessions.rows.filter((r) => machineOf(r.sessionId) === section.id) : []);
	let workspaces = $derived(knownWorkspaces(ownRows));
	let picking = $state(false);

	let canTry = $derived(machine !== null && machine.status !== 'connected' && isTauri());
	let newHint = $derived(
		offline
			? `${section.title} isn't reachable`
			: workspaces.length === 0
				? firstSessionCopy(section.title)
				: `New session on ${section.title}`
	);

	function onNewHere(): void {
		if (workspaces.length === 1) {
			newSessionFrom(workspaces[0].anchor);
			return;
		}
		picking = !picking;
	}

	function startIn(anchor: string): void {
		picking = false;
		newSessionFrom(anchor);
	}
</script>

<section class="computer" aria-label={section.title}>
	<div class="computer-head">
		{#if machine !== null}
			<span
				class="status-dot status-{statusTone(machine.status)}"
				role="img"
				aria-label={STATUS_WORDS[machine.status]}
				title={STATUS_WORDS[machine.status]}
			></span>
		{/if}
		<span class="computer-name" title={section.title}>{section.title}</span>
		{#if remote && machine !== null}
			<div class="computer-actions">
				{#if canTry && machine !== null}
					<button
						class="head-btn"
						type="button"
						title={`Try reaching ${section.title} now`}
						onclick={() => machine !== null && reconnectMachine(machine.id)}>Try now</button
					>
				{/if}
				<button
					class="head-btn"
					type="button"
					title={newHint}
					aria-label={newHint}
					aria-expanded={workspaces.length > 1 ? picking : undefined}
					disabled={offline || workspaces.length === 0}
					onclick={onNewHere}
				>
					<Icon name="plus" size={12} />
					<span>New here</span>
				</button>
			</div>
		{/if}
	</div>
	{#if note !== null}
		<p class="computer-note" role="status">{note}</p>
	{/if}
	{#if picking && workspaces.length > 1 && !offline}
		<div class="picker" role="group" aria-label={`Start a session on ${section.title} in`}>
			{#each workspaces as ws (ws.path)}
				<button class="pick" type="button" title={ws.path} onclick={() => startIn(ws.anchor)}>
					<Icon name="folder" size={13} />
					<span class="pick-name">{workspaceName(ws.path)}</span>
				</button>
			{/each}
		</div>
	{/if}

	{#each groups as group (group.id)}
		<div class="group" role="group" aria-label={group.label}>
			<span class="group-label">{group.label}</span>
			{#each group.rows as row (row.sessionId)}
				<RailSessionRow
					{row}
					{remote}
					{offline}
					active={row.sessionId === activeId}
					onselect={() => onselect(row.sessionId)}
				/>
			{/each}
		</div>
	{:else}
		<EmptyState
			compact
			body={remote && machine !== null && ownRows.length === 0 ? firstSessionCopy(section.title) : emptyCopy}
		/>
	{/each}
</section>

<style>
	.computer {
		display: flex;
		flex-direction: column;
	}

	.computer + :global(.computer) {
		margin-top: var(--space-2);
		border-top: var(--border-width) solid var(--color-hairline);
	}

	.computer-head {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-2) var(--space-2) var(--space-1);
		min-width: 0;
	}

	.computer-name {
		flex: 1;
		min-width: 0;
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.status-dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		flex-shrink: 0;
	}
	.status-ok {
		background: var(--color-ok);
	}
	.status-info {
		background: var(--color-accent);
	}
	.status-warn {
		background: var(--color-warn);
	}
	.status-muted {
		background: var(--color-ink-muted);
	}

	.computer-actions {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		flex-shrink: 0;
	}

	.head-btn {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		border: none;
		background: transparent;
		border-radius: var(--radius-sm);
		padding: 0 var(--space-1);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		cursor: pointer;
		line-height: var(--leading-relaxed);
	}

	.head-btn:hover:not(:disabled) {
		background: var(--color-lifted);
		color: var(--color-ink);
	}

	.head-btn:disabled {
		color: var(--color-ink-muted);
		cursor: default;
	}

	.computer-note {
		margin: 0;
		padding: 0 var(--space-2) var(--space-1);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.picker {
		display: flex;
		flex-direction: column;
		padding: 0 var(--space-1) var(--space-1);
	}

	.pick {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		border: none;
		background: transparent;
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-2);
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink);
		cursor: pointer;
		text-align: left;
		min-width: 0;
	}

	.pick:hover {
		background: var(--color-lifted);
	}

	.pick-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.group {
		display: flex;
		flex-direction: column;
	}

	.group-label {
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		padding: var(--space-2) var(--space-2) var(--space-1);
	}
</style>
