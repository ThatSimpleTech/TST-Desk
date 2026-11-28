<script lang="ts">
	// Settings, Policy: the folders whose own instructions load (SECUX2-01),
	// so the once-per-project question can be undone in one place.
	import ConfirmButton from './ConfirmButton.svelte';
	import { revokeFolderTrust, settings } from '../settings.svelte.js';

	const LABELS: Record<string, string> = {
		instructions: 'instructions',
		rules: 'rules',
		skills: 'skills',
		workers: 'workers'
	};
</script>

<section class="folders" aria-label="Trusted project folders">
	<p class="title">Trusted project folders</p>
	<p class="hint">
		A project's own AGENTS.md, rules, skills and workers load only in these folders. Revoke one and
		its next chat asks again.
	</p>
	{#if settings.trustedFolders.length === 0}
		<p class="empty">No folders trusted yet.</p>
	{:else}
		<ul class="list">
			{#each settings.trustedFolders as row (row.folder)}
				<li class="row">
					<span class="path">{row.folder}</span>
					<span class="kinds">{row.kinds.map((k) => LABELS[k] ?? k).join(', ')}</span>
					<ConfirmButton
						label="Revoke"
						buttonClass="revoke"
						question={`Stop loading the instructions in ${row.folder}?`}
						ariaLabel="Revoke trust for {row.folder}"
						onconfirm={() => revokeFolderTrust(row.folder)}
					/>
				</li>
			{/each}
		</ul>
	{/if}
</section>

<style>
	.folders {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
		margin-top: var(--space-6);
	}

	.title {
		margin: 0;
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}

	.hint,
	.empty {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.list {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.row {
		display: flex;
		align-items: center;
		gap: var(--space-3);
	}

	.path {
		flex: 1;
		min-width: 0;
		overflow-wrap: anywhere;
		font-family: var(--font-mono);
		font-size: var(--text-sm);
	}

	.kinds {
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
	}
</style>
