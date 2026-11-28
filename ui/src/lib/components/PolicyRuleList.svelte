<script lang="ts">
	import ConfirmButton from './ConfirmButton.svelte';
	import { ARM_MS, clickDecides, newArming, notePress } from '../approval-arming';
	// Policy section (TD-1703 / TD-803 / TD-804): skip-all, then saved
	// always-allow rules with revoke.
	//
	// Split from SettingsPane so that pane stays inside §6's line budget; the
	// styles here are used by nothing else, so the split costs no duplication.
	//
	// Three states, not two: rules live in a workspace, so "no session yet" is
	// a different claim from "no rules saved" and reads differently.
	import EmptyState from './EmptyState.svelte';
	import { ruleArgsText } from '../approval';
	import { staysOnCopy } from '../machines';
	import { machineNameFor } from '../machines.svelte.js';
	import { settings, revokeRule, setLoadGlobalMemory, setSkipAllApprovals } from '../settings.svelte.js';

	import { permissionCopy } from '../phone-permissions';

	interface Props {
		/** The attached session, or null when no workspace is open. */
		sessionId: string | null;
		/** False on a phone the computer has not let turn approvals off
		 *  (TD-5283). Turning them back on is always allowed. */
		canSkipAll?: boolean;
	}
	let { sessionId, canSkipAll = true }: Props = $props();
	let confirmingSkipAll = $state(false);
	let skipArming = $state(newArming());

	function toggleSkipAll(): void {
		if (settings.skipAllApprovals) {
			setSkipAllApprovals(false);
			return;
		}
		confirmingSkipAll = true;
		skipArming = newArming();
		setTimeout(() => (skipArming = { ...skipArming, armed: true }), ARM_MS);
	}

	function confirmSkipAll(event: MouseEvent): void {
		if (!clickDecides(skipArming, event.detail)) return;
		confirmingSkipAll = false;
		setSkipAllApprovals(true);
	}
	const skipAllOffNote = permissionCopy('approval_switches')?.offOnPhone ?? '';
	// TD-5015: another computer's workspace keeps its own rules.
	let remoteName = $derived(machineNameFor(sessionId));
</script>

<div class="skip-all">
	<div>
		<p class="skip-title">Dangerously skip permissions</p>
		<p class="hint">
			Skip everything. Every ask runs, including shell and Class C.
			Caps do not pause. A never rule still refuses. The filesystem
			boundary still blocks steering-file writes through the fs tools.
			The classifier and the ledger still run.
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={settings.skipAllApprovals}
		type="button"
		role="switch"
		aria-checked={settings.skipAllApprovals}
		disabled={!settings.skipAllApprovals && !canSkipAll}
		onclick={toggleSkipAll}
		>{settings.skipAllApprovals ? 'On' : 'Off'}</button
	>
</div>
{#if confirmingSkipAll}
	<!-- APPROVE-05: turning approvals off is one deliberate step, not one
	     click; turning them back on needs no confirmation. -->
	<div class="skip-confirm" role="alertdialog" aria-label="Turn off approvals?">
		<p class="skip-question">
			Turn off approvals for every session on this computer? Commands and file
			changes then run without anyone confirming them, including Class C and ones
			outside the project folder, until you turn this back on.
		</p>
		<div class="skip-actions">
			<button
				class="choice choice--danger"
				class:arming={!skipArming.armed}
				type="button"
				aria-disabled={!skipArming.armed}
				onpointerdown={() => (skipArming = notePress(skipArming))}
				onclick={confirmSkipAll}>Turn off approvals</button
			>
			<button class="choice" type="button" onclick={() => (confirmingSkipAll = false)}
				>Keep asking</button
			>
		</div>
	</div>
{/if}
{#if !settings.skipAllApprovals && !canSkipAll}
	<!-- TD-5283: a phone turns this on only if the computer allowed it. -->
	<p class="hint off-note">{skipAllOffNote}</p>
{/if}

<div class="skip-all">
	<div>
		<p class="skip-title">Load global memory</p>
		<p class="hint">
			When on, notes in ~/.tstdesk/memory/ load after this folder's
			memory. Off never reads that directory. Those files are never
			committed here.
		</p>
	</div>
	<button
		class="choice"
		class:choice--active={settings.loadGlobalMemory}
		type="button"
		role="switch"
		aria-checked={settings.loadGlobalMemory}
		onclick={() => setLoadGlobalMemory(!settings.loadGlobalMemory)}
		>{settings.loadGlobalMemory ? 'On' : 'Off'}</button
	>
</div>

{#if sessionId === null}
	<EmptyState align="start" body="Open a workspace to see the rules saved for it." />
{:else if remoteName !== null}
	<EmptyState align="start" body={staysOnCopy(remoteName)} />
{:else if settings.rules.length === 0}
	<EmptyState align="start" body="No always-allow rules saved for this workspace." />
{:else}
	<ul class="rules">
		{#each settings.rules as rule (rule.tool + rule.args)}
			<li class="rule">
				<span class="rule-text">
					<span class="rule-tool">{rule.tool}</span>
					<span class="rule-args" title={rule.args}>{ruleArgsText(rule)}</span>
				</span>
				<span class="rule-effect">{rule.effect}</span>
				<ConfirmButton
					label="Revoke"
					buttonClass="revoke"
					question={`Revoke the ${rule.effect} rule for ${rule.tool} ${ruleArgsText(rule)}?`}
					ariaLabel="Revoke {rule.tool} {ruleArgsText(rule)}"
					onconfirm={() => revokeRule(rule.tool, rule.args)}
				/>
			</li>
		{/each}
	</ul>
{/if}

<style>
	.skip-confirm {
		margin: var(--space-2) 0 var(--space-3);
		padding: var(--space-3);
		border: 1px solid var(--color-err);
		border-radius: var(--radius-sm);
	}

	.skip-question {
		margin: 0 0 var(--space-2);
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.skip-actions {
		display: flex;
		gap: var(--space-2);
	}

	.choice--danger {
		border-color: var(--color-err);
		color: var(--color-err);
	}

	.arming {
		opacity: 0.55;
		cursor: default;
	}

	.skip-all {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-3);
		margin-bottom: var(--space-4);
		padding-bottom: var(--space-4);
		border-bottom: 1px solid var(--color-hairline);
	}

	.skip-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
		margin: 0 0 var(--space-1);
	}

	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: 0;
	}

	.choice {
		flex-shrink: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.choice--active {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border-color: var(--color-accent);
	}

	.rules {
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.rule {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		padding: var(--space-2) 0;
		border-bottom: 1px solid var(--color-hairline);
	}

	.rule-text {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
	}

	.rule-tool {
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.rule-args {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.rule-effect {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}
</style>
