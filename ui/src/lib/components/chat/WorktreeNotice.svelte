<script lang="ts">
	// A fresh chat's git worktree, on its start screen (TD-5126): the branch it
	// works on, the main checkout's uncommitted changes it won't have (with a
	// way back to the main checkout), and the setup command's progress.
	import Icon from "../Icon.svelte";
	import type { SessionWorktreeInfo } from "../../protocol";
	import { startInMainCheckout } from "../../session-actions.svelte.js";
	import { worktreeNotice } from "../../worktree-notice";

	let { sessionId, worktree }: { sessionId: string; worktree: SessionWorktreeInfo } = $props();

	const notice = $derived(worktreeNotice(worktree));
</script>

<div class="worktree" role="note" aria-label="Worktree">
	<p class="where"><Icon name="branch" size={13} /><span>{notice.where}</span></p>
	{#if notice.dirty !== null}
		<p class="dirty">{notice.dirty}</p>
		<!-- The daemon refuses to close a worktree while its setup runs. -->
		<button
			class="chip"
			type="button"
			disabled={worktree.setup === "running"}
			title={worktree.setup === "running" ? "Available once the setup finishes." : undefined}
			onclick={() => startInMainCheckout(sessionId)}
		>
			Start in the main checkout instead
		</button>
	{/if}
	{#if notice.setup !== null}
		<p class="setup" class:setup--warn={notice.setupWarn}>{notice.setup}</p>
	{/if}
</div>

<style>
	.worktree {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-1);
		max-width: 32rem;
		color: var(--color-ink-muted);
		font-size: var(--text-sm);
		text-align: center;
	}

	.where {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		margin: 0;
	}

	.dirty,
	.setup {
		margin: 0;
	}

	.setup--warn {
		color: var(--color-warn);
		white-space: pre-wrap;
	}

	.chip {
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		cursor: pointer;
	}

	.chip:disabled {
		cursor: default;
		opacity: 0.6;
	}

	.chip:hover:not(:disabled) {
		color: var(--color-ink);
		background: var(--color-lifted);
	}
</style>
