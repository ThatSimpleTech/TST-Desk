<script lang="ts">
	// Presentational timeline row: a collapsed one-liner (kind glyph, kind
	// label, title, preview) that expands to full details — arguments, output,
	// a syntax-highlighted diff for file writes, and the live shell stream.
	// All visual decisions come from entry-view.ts (AC #6); this stays a view.
	import Icon from './Icon.svelte';
	import DiffPreview from './DiffPreview.svelte';
	import type { TimelineEntry } from '../timeline';
	import type { WorkerStep } from '../protocol';
	import { formatWorkerTranscript } from '../worker-transcript';
	import { KIND_LABELS, entryIcon, entryTone, isQuietEntry, isScalarDetail } from '../entry-view';

	interface Props {
		entry: TimelineEntry;
		expanded: boolean;
		ontoggle: () => void;
	}

	let { entry, expanded, ontoggle }: Props = $props();

	let tone = $derived(entryTone(entry));
	let icon = $derived(entryIcon(entry));
	let kindLabel = $derived(KIND_LABELS[entry.kind]);
	let quiet = $derived(isQuietEntry(entry));

	let diff = $derived(typeof entry.details.diff === 'string' ? (entry.details.diff as string) : null);
	// A delegate's worker steps (TD-5117), set only when the daemon sent some.
	let transcript = $derived(
		Array.isArray(entry.details.transcript)
			? formatWorkerTranscript(entry.details.transcript as WorkerStep[])
			: null
	);

	// Output, diff, transcript and name have their own places in the row;
	// the rest is a key/value grid — scalars inline, structures in a code block.
	let fields = $derived(
		Object.entries(entry.details).filter(
			([key]) => key !== 'output' && key !== 'diff' && key !== 'name' && key !== 'transcript'
		)
	);

	function pretty(value: unknown): string {
		if (typeof value === 'string') return value;
		return JSON.stringify(value, null, 2);
	}

	function inline(value: string | number | boolean | null): string {
		return value === null ? '—' : String(value);
	}
</script>

<div class="row row--{tone}" class:row--quiet={quiet} class:expanded>
	<button class="row-main" type="button" onclick={ontoggle} aria-expanded={expanded}>
		<span class="glyph" aria-hidden="true"><Icon name={icon} size={14} /></span>
		<span class="kind">{kindLabel}</span>
		<span class="title">{entry.title}</span>
		<span class="preview">{entry.preview}</span>
		{#if entry.progress}<span class="progress" aria-live="polite">{entry.progress}</span>{/if}
		<span class="chevron" class:chevron--open={expanded} aria-hidden="true">
			<Icon name="chevron-right" size={14} />
		</span>
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
					<DiffPreview {diff} />
				</section>
			{/if}

			{#if transcript}
				<section class="section">
					<h4 class="section-title">Worker transcript</h4>
					<pre class="code">{transcript}</pre>
				</section>
			{/if}

			{#if fields.length > 0}
				<dl class="fields">
					{#each fields as [key, value] (key)}
						<dt class="field-key">{key}</dt>
						{#if isScalarDetail(value)}
							<dd class="field-value field-value--inline">{inline(value)}</dd>
						{:else}
							<dd class="field-value"><pre class="code">{pretty(value)}</pre></dd>
						{/if}
					{/each}
				</dl>
			{/if}
		</div>
	{/if}
</div>

<style>
	.row {
		/* Collapsed height must equal ActivityTimeline's ROW_HEIGHT (32px).
		   border-box (global) includes the border, so the total stays exact. */
		height: 32px;
		border-bottom: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
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
		color: var(--color-ink);
		font-size: var(--text-xs);
		cursor: pointer;
		text-align: left;
		/* A quiet row's narrow layout asks how wide the row is (TD-5150). */
		container-type: inline-size;
	}

	.row-main:hover {
		background: var(--color-sunken);
	}

	/* Keep the ring inside the 32px row; the virtual list clips outside it. */
	.row-main:focus-visible {
		outline-offset: -2px;
	}

	/* One glyph per entry kind, coloured by outcome (AC #6): the shape says
	   what happened, the colour says how it went. */
	.glyph {
		display: inline-flex;
		flex-shrink: 0;
		color: var(--color-ink-muted);
	}

	.row--info .glyph { color: var(--color-accent); }
	.row--success .glyph { color: var(--color-ok); }
	.row--warning .glyph { color: var(--color-warn); }
	.row--danger .glyph { color: var(--color-err); }

	.kind {
		color: var(--color-ink-secondary);
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
		color: var(--color-ink-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
	}

	/* Bookkeeping rows (a tool-output trim, TD-5150) step back: muted words,
	   regular weight, so a compaction still stands out among them. Their
	   title is the numbers ("3 results · 1.2k tokens freed"), set in the
	   UI face, not mono, so it is short. It takes its full width before the
	   preview gets any (the preview's basis is 0), and when the row cannot
	   hold it the tail is cut, never the numbers at its start. Measured in
	   e2e/trim-row.spec.ts at the phone's 200px pane and the desktop's 320px. */
	.row--quiet .kind,
	.row--quiet .title,
	.row--quiet .preview {
		color: var(--color-ink-muted);
		font-weight: var(--weight-normal);
	}

	.row--quiet .title {
		font-family: var(--font-sans);
		max-width: none;
		flex: 0 1 auto;
		min-width: 0;
	}

	/* Below 360px (the phone's 200px pane, the desktop's default 320px)
	   the preview would be a stub like "Trimm…": it goes, and its gap goes
	   to the title. Wider panes show "Trimmed tool output · before → after". */
	@container (max-width: 359px) {
		.row--quiet .preview {
			display: none;
		}
	}

	.progress {
		color: var(--color-ink-secondary);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		flex: none;
	}

	.chevron {
		display: inline-flex;
		color: var(--color-ink-muted);
		flex-shrink: 0;
		transition: transform var(--dur-exit) var(--ease-out);
	}

	.chevron--open {
		transform: rotate(90deg);
	}

	.details {
		padding: var(--space-3);
		display: flex;
		flex-direction: column;
		gap: var(--space-3);
		background: var(--color-sunken);
		animation: rise var(--dur-enter) var(--ease-out);
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.section-title {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-ink-secondary);
		margin: 0;
	}

	.code {
		margin: 0;
		padding: var(--space-2) var(--space-3);
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		white-space: pre-wrap;
		word-break: break-word;
		overflow-x: auto;
	}

	.code--stderr {
		color: var(--color-err);
	}

	/* The diff itself is DiffPreview, shared with the Files and Work panes
	   (AC #3) so one write looks the same wherever it is opened. */

	/* Key/value grid: keys in their own column so a dozen fields read as a
	   table, not a stack of labelled boxes. */
	.fields {
		margin: 0;
		display: grid;
		grid-template-columns: max-content minmax(0, 1fr);
		gap: var(--space-1) var(--space-3);
		align-items: baseline;
	}

	.field-key {
		font-size: var(--text-xs);
		font-weight: var(--weight-semibold);
		color: var(--color-ink-secondary);
	}

	.field-value {
		margin: 0;
		min-width: 0;
	}

	.field-value--inline {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		color: var(--color-ink);
		white-space: pre-wrap;
		word-break: break-word;
	}
</style>
