<script lang="ts">
	// Folder trust card (SECUX2-01). A project's own AGENTS.md or CLAUDE.md,
	// .tst/rules, skills and workers load only once the person trusts the
	// folder. Until then the chat runs without them. Trust covers edits to the
	// kinds of file listed; a new kind asks again with only the new files.
	import { decideFolderTrust, followingRemoteSession, session } from '../session-status.svelte.js';

	const KIND_LABELS: Record<string, string> = {
		instructions: 'Instructions',
		rules: 'Rule',
		skills: 'Skill',
		workers: 'Worker'
	};

	const trust = $derived(session.folderTrust);
	const remote = $derived(followingRemoteSession());
	const added = $derived((trust?.trusted_kinds?.length ?? 0) > 0);
	let reviewing = $state(false);
	$effect(() => {
		void session.folderTrust;
		reviewing = false;
	});
</script>

{#if trust !== null}
	<footer class="trust-bar" aria-label="Project instructions trust">
		{#if trust.state === 'pending' || reviewing}
			<article class="card" aria-label="Load this project's instructions?">
				<header class="card-head">
					<span class="card-title"
						>{added ? 'New instructions in this project' : "Load this project's instructions?"}</span
					>
					<span class="badge">{trust.files.length} file{trust.files.length === 1 ? '' : 's'}</span>
				</header>
				<p class="lede">
					This folder has its own instructions for the agent. They go into every prompt and can
					steer what it does, so they load only once you trust the folder. Until then this chat
					runs without them. Trust covers later edits to these kinds of file.
				</p>
				<ul class="rules">
					{#each trust.files as file (file.path)}
						<li class="rule">
							<span class="rule-key">{KIND_LABELS[file.kind] ?? file.kind}</span>
							<code>{file.path}</code>
						</li>
					{/each}
				</ul>
				{#if remote}
					<p class="lede">Decide on the computer this session runs on.</p>
				{:else}
					<footer class="card-actions">
						<button class="btn btn--accept" type="button" onclick={() => decideFolderTrust(true)}
							>Trust this folder</button
						>
						<button class="btn btn--reject" type="button" onclick={() => decideFolderTrust(false)}
							>Run without them</button
						>
					</footer>
				{/if}
			</article>
		{:else}
			<div class="notice">
				<span>This project's own instructions are not loaded. You chose to run without them.</span>
				<button class="btn btn--link" type="button" onclick={() => (reviewing = true)}>Review</button>
			</div>
		{/if}
	</footer>
{/if}

<style>
	.trust-bar {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-hairline);
		background: var(--color-lifted);
		max-height: 40%;
		overflow-y: auto;
		flex-shrink: 0;
	}
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		padding: var(--space-4);
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-left: var(--space-1) solid var(--color-warn);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-sm);
	}
	.card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
	}
	.card-title {
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		color: var(--color-ink);
	}
	.badge {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		padding: var(--space-1) var(--space-2);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
		color: var(--color-ink-secondary);
		flex-shrink: 0;
	}
	.lede {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: var(--leading-normal);
	}
	.rules {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}
	.rule {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 2fr) minmax(0, 1fr);
		gap: var(--space-2);
		padding: var(--space-2) var(--space-3);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-size: var(--text-xs);
	}
	.rule-key {
		font-weight: var(--weight-semibold);
	}
	.notice {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}
	.card-actions {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-2);
	}
	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-4);
		border: var(--border-width) solid transparent;
		border-radius: var(--radius-md);
		cursor: pointer;
	}
	.btn--accept {
		background: var(--color-accent);
		color: var(--color-on-accent);
	}
	.btn--reject {
		background: transparent;
		color: var(--color-ink);
		border-color: var(--color-hairline);
	}
	.btn--link {
		background: transparent;
		color: var(--color-accent);
		padding: var(--space-1) var(--space-2);
	}
</style>
