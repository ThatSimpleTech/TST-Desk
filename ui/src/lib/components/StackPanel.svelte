<script lang="ts">
	// Resolved instruction-stack panel (TD-1201).
	//
	// Presentational view over stack-store: sources arrive in precedence
	// order from the daemon, per-file token counts and total come from the
	// same payload, and the cache badge reports the provider-reported state
	// (never inferred here — "unknown until a turn runs" and "provider
	// reports no cache figure" are both real answers, and neither is a
	// miss).
	// The loop pushes a fresh stack when steering changes at a turn
	// boundary (TD-509). Subscribe and refresh live in AppShell
	// (TD-1204) — this panel is only in the DOM on the Stack tab, so
	// owning the subscriber here dropped every reply that arrived
	// while the user was on Activity, and raced refresh ahead of
	// subscribe on first open. Clicking a file opens it in the system
	// editor via tauri-plugin-opener.
	import { stack } from '../stack-store.svelte.js';
	import { cacheBadge, cacheLabel, formatTokens, memoryPlaceholderCopy, memoryReasonLabel, warningBadge } from '../stack-store';
	import { openInEditor } from '../open-file';
	import Icon from './Icon.svelte';

	const badge = $derived(cacheBadge(stack.lastCachedTokens, stack.cacheObserved));

	function open(path: string): void {
		void openInEditor(path);
	}

	function baseName(path: string): string {
		return path.split('/').pop() ?? path;
	}
</script>

<div class="stack-panel">
	{#if !stack.loaded}
		<p class="empty">No instruction stack yet.</p>
	{:else}
		<ul class="sources">
			{#each stack.sources as entry (entry.path)}
				<li class="source" class:inactive={!entry.active}>
					<button class="file" onclick={() => open(entry.path)} title={entry.path}>
						<span class="name">{baseName(entry.path)}</span>
						<span class="tokens">{formatTokens(entry.tokens)} tok</span>
					</button>
					<div class="meta">
						<span class="precedence">{entry.precedence}</span>
						{#if !entry.active}
							<span class="chip chip-off">not in prompt</span>
						{/if}
						{#if entry.applies_to}
							<!-- TD-503's touch-tracking is not plumbed yet (no caller
							     passes matched_paths), so scoped rules are always
							     loaded. Show prompt membership, not a match verdict;
							     'matched/unmatched' lands with the tracking. -->
							{#if entry.active}
								<span class="chip chip-on">in prompt</span>
							{/if}
							<span class="globs" title="appliesTo — path-scoped rule">{entry.applies_to.join(', ')}</span>
						{/if}
						{#if entry.subtree}
							<span class="scope">subtree {entry.subtree}</span>
						{/if}
						{#if entry.is_fallback}
							<span class="chip chip-note">CLAUDE.md fallback</span>
						{/if}
						{#if entry.shadowed_path}
							<span class="chip chip-note" title={entry.shadowed_path}>
								shadows {baseName(entry.shadowed_path)}
							</span>
						{/if}
						{#each entry.warnings as warning}
							<span class="chip chip-warn" title={warning}
								><Icon name="alert" size={11} /> {warningBadge(warning)}</span
							>
						{/each}
					</div>
					{#if entry.imports && entry.imports.length > 0}
						<ul class="imports">
							{#each entry.imports as imp (imp.path)}
								<li style={`--depth: ${imp.depth}`}>
									<button class="file import" onclick={() => open(imp.path)} title={imp.path}>
										<span class="name">{baseName(imp.path)}</span>
									</button>
									{#if imp.issue}
										<span class="chip chip-warn" title={imp.issue}
											><Icon name="alert" size={11} /> import issue</span
										>
									{/if}
								</li>
							{/each}
						</ul>
					{/if}
				</li>
			{/each}
		</ul>
		{#if stack.skills.length > 0}
			<section class="memory" aria-label="Skills">
				<h2 class="memory-head">Skills</h2>
				<ul class="sources">
					{#each stack.skills as skill (skill.name)}
						<li class="source" class:inactive={!skill.loaded}>
							<div class="file">
								<span class="name">{skill.name}</span>
								<span class="tokens">{formatTokens(skill.tokens)} tok</span>
							</div>
							<div class="meta">
								<span class="precedence">{skill.source}</span>
								{#if skill.loaded}
									<span class="chip chip-on">loaded</span>
								{:else}
									<span class="chip chip-off">catalog</span>
								{/if}
								{#if skill.description}
									<span class="globs">{skill.description}</span>
								{/if}
							</div>
						</li>
					{/each}
				</ul>
			</section>
		{/if}
		<section class="memory" aria-label="Memory">
			<h2 class="memory-head">Memory</h2>
			{#if stack.memoryPlaceholder && stack.memory.length === 0}
				<p class="memory-empty">{memoryPlaceholderCopy()}</p>
			{:else}
				<ul class="sources">
					{#each stack.memory as entry (entry.path)}
						<li class="source">
							<button class="file" onclick={() => open(entry.path)} title={entry.path}>
								<span class="name">{baseName(entry.path)}</span>
								<span class="tokens">{formatTokens(entry.tokens)} tok</span>
							</button>
							<div class="meta">
								<span class="chip chip-note">{memoryReasonLabel(entry.reason)}</span>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
			{#if stack.memoryDropped.length > 0}
				<h3 class="memory-head">Dropped</h3>
				<ul class="sources">
					{#each stack.memoryDropped as entry (entry.path)}
						<li class="source inactive">
							<button class="file" onclick={() => open(entry.path)} title={entry.path}>
								<span class="name">{baseName(entry.path)}</span>
								<span class="tokens">{formatTokens(entry.tokens)} tok</span>
							</button>
							<div class="meta">
								<span class="chip chip-off">{memoryReasonLabel(entry.reason)}</span>
								<span class="chip chip-off">budget</span>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
		<footer class="totals">
			<span class="total">{formatTokens(stack.totalTokens)} tokens ({stack.tokenMethod})</span>
			<span
				class="cache"
				class:cache-hit={badge === 'hit'}
				class:cache-miss={badge === 'miss'}
			>
				{cacheLabel(stack.lastCachedTokens, stack.cacheObserved)}
			</span>
		</footer>
	{/if}
</div>

<style>
	.stack-panel {
		height: 100%;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
	}

	.empty {
		padding: var(--space-6);
		color: var(--color-text-muted);
		font-size: var(--text-sm);
		text-align: center;
	}

	.sources {
		flex: 1;
		list-style: none;
		margin: 0;
		padding: var(--space-2) 0;
	}

	.source {
		padding: var(--space-2) var(--space-4);
		border-bottom: var(--border-width) solid var(--color-border);
	}

	.source.inactive {
		opacity: 0.6;
	}

	.file {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--space-3);
		width: 100%;
		padding: 0;
		border: none;
		background: none;
		color: var(--color-text);
		font-size: var(--text-sm);
		font-weight: var(--weight-semibold);
		cursor: pointer;
		text-align: left;
	}

	.file:hover .name {
		text-decoration: underline;
	}

	.file .tokens {
		color: var(--color-text-muted);
		font-weight: var(--weight-normal);
		white-space: nowrap;
	}

	.meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--space-2);
		margin-top: var(--space-1);
		font-size: var(--text-xs);
		color: var(--color-text-muted);
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		padding: 0 var(--space-2);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-sm);
		white-space: nowrap;
	}

	.chip-on {
		color: var(--color-success);
		border-color: var(--color-success);
	}

	.chip-off {
		color: var(--color-text-muted);
	}

	.chip-warn {
		color: var(--color-warning);
		border-color: var(--color-warning);
	}

	.chip-note {
		color: var(--color-info);
		border-color: var(--color-info);
	}

	.imports {
		list-style: none;
		margin: var(--space-1) 0 0;
		padding: 0;
	}

	.imports li {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		/* Imports arrive flattened with their nesting depth — re-nest
		   visually under the importer. */
		padding-left: calc(var(--depth, 1) * var(--space-4));
	}

	.file.import {
		font-weight: var(--weight-normal);
		color: var(--color-text-muted);
		width: auto;
	}

	.memory {
		border-top: var(--border-width) solid var(--color-border);
	}

	.memory-head {
		margin: 0;
		padding: var(--space-3) var(--space-4) 0;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--color-text-muted);
	}

	.memory-empty {
		padding: var(--space-2) var(--space-4) var(--space-3);
		margin: 0;
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-text-muted);
	}

	.totals {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--space-3);
		padding: var(--space-3) var(--space-4);
		border-top: var(--border-width) solid var(--color-border);
		font-size: var(--text-sm);
		position: sticky;
		bottom: 0;
		background: var(--color-bg-raised);
	}

	.cache {
		color: var(--color-text-muted);
	}

	.cache-hit {
		color: var(--color-success);
	}

	.cache-miss {
		color: var(--color-warning);
	}
</style>
