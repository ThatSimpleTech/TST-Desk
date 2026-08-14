<script lang="ts">
	// Sanitized markdown rendering (TD-1004). renderMarkdown() output is
	// DOMPurify-clean before it reaches {@html}. Copy buttons inside code
	// blocks are wired by one delegated click handler; feedback is written to
	// the clicked button directly since the markup is generated.
	import { renderMarkdown } from "../../markdown";
	// Token-driven hljs theme (TD-1609): follows the warm palette in both
	// color schemes — replaces highlight.js's light-only github.css.
	import "../../hljs-theme.css";

	let { text }: { text: string } = $props();

	function handleClick(event: MouseEvent): void {
		const btn = (event.target as HTMLElement).closest("[data-copy-btn]");
		if (btn === null) return;
		const code = btn.closest(".code-block")?.querySelector("code");
		navigator.clipboard
			.writeText(code?.textContent ?? "")
			.then(() => {
				btn.textContent = "Copied!";
				setTimeout(() => {
					btn.textContent = "Copy";
				}, 1500);
			})
			.catch(() => {
				btn.textContent = "Copy failed";
				setTimeout(() => {
					btn.textContent = "Copy";
				}, 1500);
			});
	}
</script>

<!-- Click delegation for the generated copy buttons: the real interactive
     element is the button inside the {@html}; the wrapper only listens. -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="markdown" onclick={handleClick}>
	{@html renderMarkdown(text)}
</div>

<style>
	.markdown {
		font-size: var(--text-base);
		line-height: var(--leading-normal);
		word-break: break-word;
	}

	/* {@html} content is outside Svelte's scoped styles — reach it with
	   :global() from a stable ancestor. */

	.markdown :global(p) {
		margin: var(--space-2) 0;
	}

	.markdown :global(ul),
	.markdown :global(ol) {
		margin: var(--space-2) 0;
		padding-left: var(--space-6);
	}

	.markdown :global(h1),
	.markdown :global(h2),
	.markdown :global(h3),
	.markdown :global(h4) {
		margin: var(--space-4) 0 var(--space-2);
		font-family: var(--font-display);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		line-height: var(--leading-tight);
	}

	.markdown :global(h1) {
		font-size: var(--text-xl);
	}

	.markdown :global(h2) {
		font-size: var(--text-lg);
	}

	.markdown :global(h3),
	.markdown :global(h4) {
		font-size: var(--text-base);
	}

	.markdown :global(code) {
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		background: var(--color-bg-subtle);
		border-radius: var(--radius-sm);
		padding: 0 var(--space-1);
	}

	.markdown :global(.code-block) {
		position: relative;
		margin: var(--space-3) 0;
	}

	.markdown :global(.code-block pre) {
		margin: 0;
		padding: var(--space-4);
		overflow-x: auto;
		background: var(--color-bg-subtle);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
	}

	.markdown :global(.code-block pre code) {
		display: block;
		padding: 0;
		background: transparent;
	}

	.markdown :global(.copy-btn) {
		position: absolute;
		top: var(--space-2);
		right: var(--space-2);
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-text-secondary);
		background: var(--color-bg-raised);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-sm);
		cursor: pointer;
		opacity: 0;
		transition: opacity var(--transition-fast);
	}

	.markdown :global(.code-block:hover .copy-btn),
	.markdown :global(.copy-btn:focus-visible) {
		opacity: 1;
	}

	.markdown :global(blockquote) {
		margin: var(--space-3) 0;
		padding-left: var(--space-4);
		border-left: var(--space-1) solid var(--color-border);
		color: var(--color-text-secondary);
	}

	.markdown :global(table) {
		border-collapse: collapse;
		margin: var(--space-3) 0;
	}

	.markdown :global(th),
	.markdown :global(td) {
		border: var(--border-width) solid var(--color-border);
		padding: var(--space-1) var(--space-3);
		text-align: left;
	}
</style>
