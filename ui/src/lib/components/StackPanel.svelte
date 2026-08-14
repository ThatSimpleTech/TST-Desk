<script lang="ts">
	// Resolved instruction-stack panel (TD-1201).
	//
	// Presentational view over stack-store: sources arrive in precedence
	// order from the daemon, per-file token counts and total come from the
	// same payload, and the cache badge reports the provider-observed state
	// (never inferred here — "unknown until a turn runs" is a real answer).
	// The loop pushes a fresh stack when steering changes at a turn
	// boundary (TD-509), and this view re-queries on mount and session
	// switch. Clicking a file opens it in the system editor via
	// tauri-plugin-opener.
	import { onMount } from 'svelte';
	import { session } from '../session-status.svelte.js';
	import { initStack, refreshStack, stack, teardownStack } from '../stack-store.svelte.js';
	import { cacheLabel, formatTokens } from '../stack-store';
	import { openInEditor } from '../open-file';
	import Icon from './Icon.svelte';

	onMount(() => {
		initStack();
		return teardownStack;
	});

	$effect(() => {
		// Track the attached session: re-ask the daemon when it changes
		// (and once on mount).
		void session.sessionId;
		refreshStack();
	});

	function open(path: string): void {
		void openInEditor(path);
	}

	function baseName(path: string): string {
		return path.split('/').pop() ?? path;
	}

	/** Short badge for a warning line; full text stays in the tooltip. */
	function warningBadge(warning: string): string {
		return warning.includes('exceeds 200 lines') ? 'over 200 lines' : warning;
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
		<footer class="totals">
			<span class="total">{formatTokens(stack.totalTokens)} tokens ({stack.tokenMethod})</span>
			<span
				class="cache"
				class:cache-hit={(stack.lastCachedTokens ?? 0) > 0}
				class:cache-miss={stack.lastCachedTokens === 0}
			>
				{cacheLabel(stack.lastCachedTokens)}
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
