<script lang="ts">
	// Workspace spend budget. The built-in $25 pause is not shown on the
	// title bar. Saving a number here writes caps.spend_usd and is what
	// the title bar prints as "of $…".
	import { sendToDaemon } from "../connection-status.svelte.js";
	import { session } from "../session-status.svelte.js";

	let draft = $state("");
	let seeded = $state("");

	$effect(() => {
		const key = `${session.sessionId}|${session.spendConfigured}|${session.boundary?.spend_usd ?? ""}`;
		if (key === seeded) return;
		seeded = key;
		draft =
			session.spendConfigured && session.boundary !== null
				? String(session.boundary.spend_usd)
				: "";
	});

	let error = $state<string | null>(null);

	function save(amount: number | null): void {
		if (session.sessionId === null) return;
		error = null;
		if (amount !== null && (!Number.isFinite(amount) || amount < 0)) {
			error = "Enter a dollar amount, or leave it blank for no budget.";
			return;
		}
		sendToDaemon({
			type: "set_spend_cap",
			session_id: session.sessionId,
			spend_usd: amount,
		});
	}

	function commit(): void {
		const trimmed = draft.trim();
		if (trimmed === "") {
			save(null);
			return;
		}
		save(Number(trimmed));
	}
</script>

{#if session.sessionId === null}
	<p class="hint">Open a workspace to set its budget.</p>
{:else}
	<p class="hint">
		The session spend limit for this workspace. Blank means no budget: the title bar does not
		show one. A built-in pause at $25 still stops a session that spends that much. Saving a
		number replaces that pause and shows it next to the cost.
	</p>
	<label class="field">
		<span class="field-name">Spend</span>
		<input
			class="input"
			type="text"
			inputmode="decimal"
			placeholder="No budget"
			bind:value={draft}
			onkeydown={(e) => {
				if (e.key === "Enter") commit();
			}}
		/>
	</label>
	<div class="actions">
		<button class="save" type="button" onclick={commit}>Save</button>
		{#if session.spendConfigured}
			<button class="clear" type="button" onclick={() => save(null)}>Clear</button>
		{/if}
	</div>
	{#if error !== null}
		<p class="error" role="alert">{error}</p>
	{/if}
{/if}

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: 0;
	}

	.field {
		display: grid;
		grid-template-columns: 6rem 1fr;
		align-items: center;
		gap: var(--space-3);
		margin-top: var(--space-4);
	}

	.field-name {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.input {
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-ground);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-2);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		margin-top: var(--space-3);
	}

	.save,
	.clear {
		font-size: var(--text-sm);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.save {
		color: var(--color-on-accent);
		background: var(--color-accent);
		border: none;
	}

	.clear {
		color: var(--color-ink-secondary);
		background: transparent;
		border: 1px solid var(--color-hairline);
	}

	.error {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		color: var(--color-err);
	}
</style>
