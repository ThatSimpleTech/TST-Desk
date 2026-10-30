<script lang="ts">
	// A free-form JSON value (TD-5203): a tier's extra_body, a language
	// server's init_options. Edited as JSON text and saved on leaving the box,
	// or Ctrl+Enter; text that does not parse is never sent, and goes when the
	// stored value changes under it. Escape drops a draft (and only then stays
	// in Settings). Empty is "not set" for a nullable key. A sent draft stays
	// until the daemon answers.
	import { untrack } from 'svelte';
	import type { ConfigNode, JsonValue } from '../../protocol';
	import { deepEqual, jsonText, parseJsonText } from '../../config-editor.js';

	let {
		id,
		node,
		value,
		pending,
		disabled,
		onsave,
	}: {
		id: string;
		node: ConfigNode;
		value: JsonValue | undefined;
		pending: boolean;
		disabled: boolean;
		onsave: (value: JsonValue) => void;
	} = $props();

	let draft = $state<string | null>(null);
	let sent = $state(false);
	let error = $state('');
	let box = $state<HTMLTextAreaElement | null>(null);
	/** The stored value this box last saw; a change of it retires an unsent draft. */
	let seen: { value: JsonValue | undefined } | null = null;

	$effect(() => {
		if (sent && !pending) {
			draft = null;
			sent = false;
		}
	});

	$effect(() => {
		const stored = value;
		untrack(() => {
			const changed = seen !== null && !deepEqual(seen.value, stored);
			seen = { value: stored };
			if (changed && draft !== null && !sent && document.activeElement !== box) {
				draft = null;
				error = '';
			}
		});
	});

	const shown = $derived(draft ?? jsonText(value));

	function commit(): void {
		if (draft === null) return;
		const parsed = parseJsonText(draft, node);
		if (!parsed.ok) {
			error = parsed.error;
			return;
		}
		error = '';
		if (deepEqual(parsed.value, value ?? null)) {
			draft = null;
			return;
		}
		sent = true;
		onsave(parsed.value);
	}
</script>

<textarea
	bind:this={box}
	{id}
	class="cfg-input"
	spellcheck="false"
	{disabled}
	value={shown}
	placeholder={node.nullable ? 'not set' : '{}'}
	aria-invalid={error ? 'true' : undefined}
	aria-describedby={error ? `${id}-error` : undefined}
	oninput={(e) => (draft = e.currentTarget.value)}
	onblur={commit}
	onkeydown={(e) => {
		if (e.key === 'Escape') {
			if (draft === null && !error) return;
			// The draft goes; Settings closes on an Escape with nothing to drop.
			e.stopPropagation();
			e.preventDefault();
			draft = null;
			error = '';
		} else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
			e.preventDefault();
			commit();
		}
	}}
></textarea>
{#if error}
	<p id="{id}-error" class="cfg-error" role="alert">{error}</p>
{/if}
