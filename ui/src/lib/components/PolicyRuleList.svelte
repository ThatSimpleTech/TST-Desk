<script lang="ts">
	// Policy section (TD-1703 / TD-803 / TD-804): skip-all, then saved
	// always-allow rules with revoke.
	//
	// Split from SettingsPane so that pane stays inside §6's line budget; the
	// styles here are used by nothing else, so the split costs no duplication.
	//
	// Three states, not two: rules live in a workspace, so "no session yet" is
	// a different claim from "no rules saved" and reads differently.
	import EmptyState from './EmptyState.svelte';
	import { settings, revokeRule, setLoadGlobalMemory, setSkipAllApprovals } from '../settings.svelte.js';

	interface Props {
		/** The attached session, or null when no workspace is open. */
		sessionId: string | null;
	}
	let { sessionId }: Props = $props();
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
		onclick={() => setSkipAllApprovals(!settings.skipAllApprovals)}
		>{settings.skipAllApprovals ? 'On' : 'Off'}</button
	>
</div>

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
{:else if settings.rules.length === 0}
	<EmptyState align="start" body="No always-allow rules saved for this workspace." />
{:else}
	<ul class="rules">
		{#each settings.rules as rule (rule.tool + rule.args)}
			<li class="rule">
				<span class="rule-text">
					<span class="rule-tool">{rule.tool}</span>
					<span class="rule-args">{rule.args}</span>
				</span>
				<span class="rule-effect">{rule.effect}</span>
				<button
					class="revoke"
					type="button"
					aria-label="Revoke {rule.tool} {rule.args}"
					onclick={() => revokeRule(rule.tool, rule.args)}>Revoke</button
				>
			</li>
		{/each}
	</ul>
{/if}

<style>
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

	.revoke {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.revoke:hover {
		border-color: var(--color-accent);
	}
</style>
