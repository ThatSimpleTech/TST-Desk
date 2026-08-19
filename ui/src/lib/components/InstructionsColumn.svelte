<script lang="ts">
	// Instructions column on the project home (TD-2802).
	//
	// Lists AGENTS.md (or CLAUDE.md) then .tst/rules/*. Click opens the
	// file in the system editor. + creates a new rule file or opens the
	// existing root file. Writes go through the human-path protocol, never
	// the agent tools. Saving in the editor is what TD-509 reloads.
	import Icon from './Icon.svelte';
	import { openInEditor } from '../open-file';
	import {
		instructionEmptyCopy,
		rootInstruction,
		ruleInstructions,
	} from '../instructions';
	import {
		beginNewRule,
		cancelNewRule,
		createRule,
		instructions,
		loadInstructions,
		setDraftName,
		startInstructions,
	} from '../instructions.svelte.js';

	let { workspacePath }: { workspacePath: string } = $props();

	startInstructions();

	$effect(() => {
		loadInstructions(workspacePath);
	});

	let root = $derived(rootInstruction(instructions.files));
	let rules = $derived(ruleInstructions(instructions.files));
	let empty = $derived(instructions.files.length === 0);
</script>

<section class="col" aria-label="Instructions">
	<div class="head">
		<h2 class="section">Instructions</h2>
		<div class="plus-wrap">
			<button
				class="icon-btn"
				type="button"
				title="Add or open instructions"
				aria-label="Add or open instructions"
				aria-expanded={instructions.naming}
				onclick={() => (instructions.naming ? cancelNewRule() : beginNewRule())}
			>
				<Icon name="plus" size={14} />
			</button>
		</div>
	</div>
	{#if instructions.naming}
		<form
			class="name-row"
			onsubmit={(e) => {
				e.preventDefault();
				createRule();
			}}
		>
			<input
				type="text"
				placeholder="rule-name"
				aria-label="New rule name"
				value={instructions.draftName}
				oninput={(e) => setDraftName(e.currentTarget.value)}
			/>
			<button class="text-btn" type="submit" disabled={instructions.draftName.trim() === ''}
				>Create rule</button
			>
			{#if root !== null}
				<button class="text-btn" type="button" onclick={() => void openInEditor(root.path)}
					>Open {root.name}</button
				>
			{/if}
		</form>
		{#if instructions.error !== null}
			<p class="err">{instructions.error}</p>
		{/if}
	{/if}
	{#if empty && !instructions.naming}
		<p class="empty">{instructionEmptyCopy()}</p>
	{:else}
		<ul class="list">
			{#if root !== null}
				<li>
					<button class="file" type="button" onclick={() => void openInEditor(root.path)}>
						<span class="name">{root.name}</span>
						<span class="kind">{root.kind === 'claude' ? 'CLAUDE.md fallback' : 'project'}</span>
					</button>
				</li>
			{/if}
			{#each rules as file (file.path)}
				<li>
					<button class="file" type="button" onclick={() => void openInEditor(file.path)}>
						<span class="name">{file.name}</span>
						<span class="kind">rule</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</section>

<style>
	.col {
		min-width: 0;
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
	}

	.section {
		margin: 0;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--color-ink-muted);
	}

	.icon-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		background: transparent;
		color: var(--color-ink-secondary);
		cursor: pointer;
		padding: var(--space-1);
		border-radius: var(--radius-sm);
		line-height: 1;
	}

	.icon-btn:hover {
		background: var(--color-lifted);
		color: var(--color-ink);
	}

	.name-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2);
		margin-top: var(--space-3);
	}

	.name-row input {
		flex: 1 1 8rem;
		min-width: 0;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		padding: var(--space-1) var(--space-2);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.text-btn {
		border: none;
		background: transparent;
		color: var(--color-accent);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		cursor: pointer;
		padding: var(--space-1);
	}

	.text-btn:disabled {
		color: var(--color-ink-muted);
		cursor: default;
	}

	.err {
		margin: var(--space-2) 0 0;
		font-size: var(--text-xs);
		color: var(--color-err);
	}

	.empty {
		margin: var(--space-3) 0 0;
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
	}

	.list {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.file {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-2);
		width: 100%;
		text-align: left;
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		border-radius: var(--radius-md);
		padding: var(--space-2) var(--space-3);
		cursor: pointer;
		color: var(--color-ink);
	}

	.file:hover {
		background: var(--color-sunken);
	}

	.name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
	}

	.kind {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}
</style>
