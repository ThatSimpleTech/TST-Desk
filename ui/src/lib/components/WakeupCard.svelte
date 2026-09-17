<script lang="ts">
	// Wake-up card (TD-4303): presentational view of autonomy_summary.
	// One control opens the ledger in the OS editor and copies the branch.
	import { session } from "../session-status.svelte.js";
	import { wakeup } from "../wakeup.svelte.js";
	import { openBranchAndLedger } from "../wakeup";

	let copied = $state(false);

	async function openBoth(): Promise<void> {
		if (wakeup.summary === null) return;
		const result = await openBranchAndLedger(wakeup.summary, session.workspacePath);
		copied = result.copied;
	}
</script>

{#if wakeup.summary}
	<section class="wakeup" aria-label="Autonomy summary">
		<header class="head">
			<h2 class="title">Autonomy stopped</h2>
			<p class="reason">{wakeup.summary.reason}</p>
		</header>
		<dl class="facts">
			<div class="row">
				<dt>Branch</dt>
				<dd><code>{wakeup.summary.branch || "(none)"}</code></dd>
			</div>
			<div class="row">
				<dt>Ledger</dt>
				<dd><code>{wakeup.summary.ledger_path}</code></dd>
			</div>
			<div class="row">
				<dt>Changed</dt>
				<dd>
					{#if wakeup.summary.changed.length === 0}
						(none)
					{:else}
						<ul>
							{#each wakeup.summary.changed as path (path)}
								<li><code>{path}</code></li>
							{/each}
						</ul>
					{/if}
				</dd>
			</div>
			{#if wakeup.summary.refusals.length > 0}
				<div class="row">
					<dt>Refusals</dt>
					<dd>
						<ul>
							{#each wakeup.summary.refusals as item, i (`${i}:${item}`)}
								<li>{item}</li>
							{/each}
						</ul>
					</dd>
				</div>
			{/if}
			{#if wakeup.summary.ledger_excerpt}
				<div class="row excerpt">
					<dt>Ledger excerpt</dt>
					<dd><pre>{wakeup.summary.ledger_excerpt}</pre></dd>
				</div>
			{/if}
		</dl>
		<button class="open" type="button" onclick={() => void openBoth()}>
			{copied ? "Opened ledger · branch copied" : "Open branch and ledger"}
		</button>
	</section>
{/if}

<style>
	.wakeup {
		margin: 0 var(--space-4) var(--space-3);
		padding: var(--space-4);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-lifted);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
	}

	.head {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.title {
		margin: 0;
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.reason {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.facts {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.row {
		display: grid;
		grid-template-columns: 7rem 1fr;
		gap: var(--space-2);
		font-size: var(--text-sm);
	}

	dt {
		margin: 0;
		color: var(--color-ink-muted);
	}

	dd {
		margin: 0;
		color: var(--color-ink);
		min-width: 0;
	}

	code {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		word-break: break-all;
	}

	ul {
		margin: 0;
		padding-left: var(--space-4);
	}

	pre {
		margin: 0;
		white-space: pre-wrap;
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}

	.open {
		align-self: flex-start;
		padding: var(--space-2) var(--space-3);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		cursor: pointer;
	}

	.open:hover {
		border-color: var(--color-ink-muted);
	}
</style>
