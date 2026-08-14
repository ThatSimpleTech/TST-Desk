<script lang="ts">
	// Presentational timeline row: a collapsed one-liner (tone marker, kind
	// label, title, preview) that expands to full details — arguments, output,
	// a syntax-highlighted diff for file writes, and the live shell stream.
	// All visual decisions come from entry-view.ts (AC #6); this stays a view.
	import type { TimelineEntry } from '../timeline';
	import { KIND_LABELS, entryTone, classifyDiffLine, diffLines } from '../entry-view';

	interface Props {
		entry: TimelineEntry;
		expanded: boolean;
		ontoggle: () => void;
	}

	let { entry, expanded, ontoggle }: Props = $props();

	let tone = $derived(entryTone(entry));
	let kindLabel = $derived(KIND_LABELS[entry.kind]);

	let diff = $derived(typeof entry.details.diff === 'string' ? (entry.details.diff as string) : null);

	function pretty(value: unknown): string {
		if (typeof value === 'string') return value;
		return JSON.stringify(value, null, 2);
	}
</script>

<div class="row row--{tone}" class:expanded>
	<button class="row-main" type="button" onclick={ontoggle} aria-expanded={expanded}>
		<span class="marker" aria-hidden="true"></span>
		<span class="kind">{kindLabel}</span>
		<span class="title">{entry.title}</span>
		<span class="preview">{entry.preview}</span>
		<span class="chevron" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
	</button>

	{#if expanded}
		<div class="details">
			{#if entry.stdout || entry.stderr}
				<section class="section">
					<h4 class="section-title">Streaming output</h4>
					{#if entry.stdout}<pre class="code">{entry.stdout}</pre>{/if}
					{#if entry.stderr}<pre class="code code--stderr">{entry.stderr}</pre>{/if}
				</section>
			{/if}

			{#if entry.details.output}
				<section class="section">
					<h4 class="section-title">Output</h4>
					<pre class="code">{entry.details.output as string}</pre>
				</section>
			{/if}

			{#if diff}
				<section class="section">
					<h4 class="section-title">Diff</h4>
					<pre class="code diff">{#each diffLines(diff) as line}<span class="diff-line diff-line--{classifyDiffLine(line)}">{line}</span>{'\n'}{/each}</pre>
				</section>
			{/if}

			<dl class="fields">
				{#each Object.entries(entry.details) as [key, value]}
					{#if key !== 'output' && key !== 'diff' && key !== 'name'}
						<div class="field">
							<dt class="field-key">{key}</dt>
							<dd class="field-value"><pre class="code">{pretty(value)}</pre></dd>
						</div>
					{/if}
				{/each}
			</dl>
		</div>
	{/if}
</div>

<style>
	.row {
		/* Collapsed height must equal ActivityTimeline's ROW_HEIGHT (32) —
		   --space-8 is 32px. border-box (global) includes the border, so the
		   total stays exact. */
		height: var(--space-8);
		border-bottom: var(--border-width) solid var(--color-border);
		background: var(--color-bg);
	}

	.row.expanded {
		height: auto;
	}

	.row-main {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		height: 100%;
		padding: 0 var(--space-3);
		border: none;
		background: transparent;
		color: var(--color-text);
		font-size: var(--text-xs);
		cursor: pointer;
		text-align: left;
	}

	.row-main:hover {
		background: var(--color-bg-subtle);
	}

	/* Distinct colored marker per entry type (AC #6). */
	.marker {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		flex-shrink: 0;
		background: var(--color-text-muted);
	}

	.row--neutral .marker { background: var(--color-text-muted); }
	.row--info .marker { background: var(--color-info); }
	.row--success .marker { background: var(--color-success); }
	.row--warning .marker { background: var(--color-warning); }
	.row--danger .marker { background: var(--color-danger); }

	.kind {
		color: var(--color-text-secondary);
		font-weight: var(--weight-semibold);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		flex-shrink: 0;
	}

	.title {
		font-weight: var(--weight-medium);
		font-family: var(--font-mono);
		flex-shrink: 0;
		max-width: 40%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.preview {
		color: var(--color-text-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
	}

	.chevron {
		color: var(--color-text-muted);
		flex-shrink: 0;
	}

	.details {
		padding: var(--space-3);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		background: var(--color-bg-subtle);
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.section-title {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-text-secondary);
		margin: 0;
	}

	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-bg);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		white-space: pre-wrap;
		word-break: break-word;
		overflow-x: auto;
	}

	.code--stderr {
		color: var(--color-danger);
	}

	/* Syntax-highlighted diff (AC #3). */
	.diff {
		white-space: pre;
	}

	.diff-line { display: inline; }
	.diff-line--add { color: var(--color-success); }
	.diff-line--del { color: var(--color-danger); }
	.diff-line--hunk { color: var(--color-info); }
	.diff-line--meta { color: var(--color-text-muted); font-weight: var(--weight-semibold); }
	.diff-line--context { color: var(--color-text-secondary); }

	.fields {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.field-key {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-text-secondary);
	}

	.field-value {
		margin: 0;
	}
</style>
