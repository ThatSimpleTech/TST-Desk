<script lang="ts">
	// Saved always-allow rules with revoke (TD-1703 policy section, TD-803).
	//
	// Split from SettingsPane so that pane stays inside §6's line budget; the
	// styles here are used by nothing else, so the split costs no duplication.
	//
	// Three states, not two: rules live in a workspace, so "no session yet" is
	// a different claim from "no rules saved" and reads differently.
	import { settings, revokeRule } from '../settings.svelte.js';

	interface Props {
		/** The attached session, or null when no workspace is open. */
		sessionId: string | null;
	}
	let { sessionId }: Props = $props();
</script>

{#if sessionId === null}
	<p class="empty">Open a workspace to see the rules saved for it.</p>
{:else if settings.rules.length === 0}
	<p class="empty">No always-allow rules saved for this workspace.</p>
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
	.empty {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
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
