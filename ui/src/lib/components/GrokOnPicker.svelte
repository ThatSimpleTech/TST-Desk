<script lang="ts">
	// TD-5258: put the Grok CLI on the model list, so Local, API and Grok
	// are one click each (TD-5239). Before this the entry could only be
	// written by hand in config.yaml. The button is always here and is held
	// with the reason when the CLI is missing, so the page reads the same on
	// every computer. The daemon checks again and writes the entry.
	import { onMount } from "svelte";
	import { onEvent, sendToDaemon } from "../connection-status.svelte.js";
	import { modelLabel } from "../effort";
	import { settings } from "../settings.svelte.js";

	let busy = $state(false);
	let problem = $state<string | null>(null);

	const onList = $derived(
		settings.presets.find((name) => settings.presetEngines[name] === "grok") ?? null,
	);

	onMount(() =>
		onEvent((event) => {
			if (!busy) return;
			if (event.type === "setup_state") {
				busy = false;
				problem = null;
			} else if (event.type === "error" && (event.session_id ?? null) === null) {
				busy = false;
				problem = event.message;
			}
		}),
	);

	function add(): void {
		problem = null;
		busy = sendToDaemon({ type: "add_grok_preset", label: "Grok" });
	}
</script>

<h2 class="sub">Grok on the model list</h2>
{#if onList !== null}
	<p class="hint">
		{modelLabel(onList, settings.presetLabels)} is on the model list. A new chat started on it
		runs on the Grok CLI, whatever the engine above is set to.
	</p>
{:else}
	<p class="hint">
		Puts Grok beside your models in the picker. It uses the Grok CLI's own model and sign-in,
		so there is no key or model to set here.
	</p>
	<button
		class="btn"
		type="button"
		disabled={busy || !settings.grokAvailable}
		title={settings.grokAvailable ? "" : "The Grok CLI was not found on this computer"}
		onclick={add}>Add Grok to your models</button
	>
	{#if !settings.grokAvailable}
		<p class="hint warn">
			The Grok CLI was not found on this computer. Install Grok Build and run
			<code>grok login</code> first.
		</p>
	{/if}
	{#if problem}
		<p class="hint warn" role="alert">{problem}</p>
	{/if}
{/if}

<style>
	.sub {
		margin: var(--space-5) 0 0;
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}

	.hint.warn {
		color: var(--color-warn);
	}

	.btn {
		margin-top: var(--space-3);
		font-size: var(--text-sm);
		padding: var(--space-1) var(--space-3);
		border-radius: var(--radius-sm);
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn:hover:not(:disabled) {
		border-color: var(--color-accent);
	}

	.btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
</style>
