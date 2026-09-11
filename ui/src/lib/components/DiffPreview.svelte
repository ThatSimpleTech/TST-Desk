<script lang="ts">
	// Highlighted unified-diff body. Shared by the timeline row (AC #3), the
	// Files pane (TD-1705) and the Work pane (TD-3203) so one write looks the
	// same wherever it is opened and no pane copies the line classifier.
	//
	// Each line is a block with its own tint, gutter-to-gutter, the way a
	// code host paints a diff; colour alone never carries the meaning, the
	// leading +/- is still in the text.
	import { classifyDiffLine, diffLines } from '../entry-view';

	let { diff }: { diff: string } = $props();
</script>

<pre class="diff"><code class="lines">{#each diffLines(diff) as line}<span class="line line--{classifyDiffLine(line)}">{line}</span>{/each}</code></pre>

<style>
	.diff {
		margin: 0;
		padding: var(--space-1) 0;
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		line-height: var(--leading-normal);
		white-space: pre;
		overflow-x: auto;
	}

	/* inline-block + min-width so a tint runs the full scroll width when a
	   line is longer than the box, not just the visible part. `font: inherit`
	   sidesteps the browser's own <code> monospace sizing. */
	.lines {
		display: inline-block;
		min-width: 100%;
		font: inherit;
	}

	.line {
		display: block;
		padding: 0 var(--space-3);
		color: var(--color-ink-secondary);
	}

	/* A blank line (between the sections of a two-target write) still takes
	   a line's height. */
	.line:empty::after {
		content: '\00a0';
	}

	.line--add {
		color: var(--color-ok);
		background: color-mix(in srgb, var(--color-ok) 12%, transparent);
	}

	.line--del {
		color: var(--color-err);
		background: color-mix(in srgb, var(--color-err) 12%, transparent);
	}

	.line--hunk {
		color: var(--color-accent);
	}

	.line--meta {
		color: var(--color-ink-muted);
		font-weight: var(--weight-semibold);
	}
</style>
