<script lang="ts">
	import { grok } from "../grok.svelte.js";
	import { settings, setEngine } from "../settings.svelte.js";

	const ENGINES = [
		{
			kind: "native" as const,
			label: "TST native",
			blurb: "The built-in three-tier loop. Uses your OpenAI-compatible presets and keys.",
		},
		{
			kind: "grok" as const,
			label: "Grok Build",
			blurb: "Spawns your installed grok CLI over ACP. The CLI stays the engine — this window is a viewer. Auth stays in ~/.grok.",
		},
	];
</script>

<p class="hint">
	Applies to new sessions. A running session keeps the engine it started with. The Grok CLI,
	<code>grok -p</code>, and <code>grok agent stdio</code> keep working independently.
</p>

<div class="choices" role="radiogroup" aria-label="Agent engine">
	{#each ENGINES as option (option.kind)}
		<button
			class="choice"
			class:choice--active={settings.engine === option.kind}
			type="button"
			role="radio"
			aria-checked={settings.engine === option.kind}
			disabled={settings.savingEngine}
			onclick={() => setEngine(option.kind)}
		>
			<span class="choice-label">{option.label}</span>
			<span class="choice-blurb">{option.blurb}</span>
		</button>
	{/each}
</div>

{#if settings.engine === "grok"}
	{#if settings.grokAvailable}
		<p class="hint">
			Grok CLI is ready. New sessions in this window use it — type in the composer.
			The terminal TUI still works on its own with <code>grok --resume</code>.
		</p>
	{:else}
		<p class="hint warn">
			Grok CLI not found. Install Grok Build, run
			<code>grok login</code>, or set <code>engine.binary</code> in config.yaml.
		</p>
	{/if}
{/if}

{#if grok.extensions.length > 0}
	<h2 class="sub">Grok CLI extensions</h2>
	<p class="hint">Read from ~/.grok. Skills, MCP servers, and plugins the CLI already knows. Secrets stay in the CLI.</p>
	<ul class="ext">
		{#each grok.extensions as item (item.kind + item.name)}
			<li>
				<span class="kind">{item.kind}</span>
				<strong>{item.name}</strong>
				{#if item.detail}
					<span class="detail">{item.detail}</span>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

{#if grok.sessions.length > 0}
	<h2 class="sub">Grok TUI sessions</h2>
	<p class="hint">On-disk conversations under ~/.grok/sessions. Resume one with grok --resume.</p>
	<ul class="ext">
		{#each grok.sessions as row (row.id)}
			<li>
				<strong>{row.title}</strong>
				<span class="detail">{row.id.slice(0, 8)} {row.cwd}</span>
			</li>
		{/each}
	</ul>
{/if}

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
		margin: 0 0 var(--space-4);
		line-height: var(--leading-normal);
	}
	.hint.warn {
		color: var(--color-err);
	}
	.choices {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}
	.choice {
		text-align: left;
		padding: var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-lifted);
		color: inherit;
		cursor: pointer;
	}
	.choice--active {
		border-color: var(--color-accent);
	}
	.choice-label {
		display: block;
		font-weight: var(--weight-semibold);
	}
	.choice-blurb {
		display: block;
		margin-top: var(--space-1);
		font-size: var(--text-sm);
		color: var(--color-ink-muted);
	}
	code {
		font-family: var(--font-mono);
		font-size: 0.9em;
	}
	.sub {
		font-size: var(--text-sm);
		margin: var(--space-4) 0 var(--space-2);
	}
	.ext {
		list-style: none;
		padding: 0;
		margin: 0;
		font-size: var(--text-sm);
	}
	.ext li {
		display: flex;
		gap: var(--space-2);
		padding: var(--space-1) 0;
		flex-wrap: wrap;
	}
	.kind {
		text-transform: uppercase;
		color: var(--color-ink-muted);
		font-size: var(--text-xs);
		min-width: 3.5rem;
	}
	.detail {
		color: var(--color-ink-muted);
	}
</style>
