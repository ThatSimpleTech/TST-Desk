<script lang="ts">
	// A number or text key (TD-5203). What is typed stays a draft until Enter
	// or leaving the field; Escape drops it. A draft that does not parse stays
	// on screen with the reason and is not sent. A sent draft stays until the
	// daemon answers, then the box shows the stored value: the new one, or the
	// old one again when the edit was refused. For a nullable key, an empty
	// box means "not set". A multi-line box saves on leaving it, or Ctrl+Enter.
	import type { ConfigNode, JsonValue } from '../../protocol';
	import { parseNumber, parseText } from '../../config-editor.js';

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

	$effect(() => {
		if (sent && !pending) {
			draft = null;
			sent = false;
		}
	});

	const numeric = $derived(node.kind === 'int' || node.kind === 'float');
	const shown = $derived(draft ?? (value === null || value === undefined ? '' : String(value)));

	function commit(): void {
		if (draft === null) return;
		const text = draft;
		const parsed =
			numeric ? parseNumber(text, node)
			: node.nullable && text === '' ? ({ ok: true, value: null } as const)
			: parseText(text, node);
		if (!parsed.ok) {
			error = parsed.error;
			return;
		}
		error = '';
		if (parsed.value === (value ?? null)) {
			draft = null;
			return;
		}
		sent = true;
		onsave(parsed.value);
	}

	function cancel(): void {
		draft = null;
		error = '';
	}

	function onkeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') cancel();
		else if (event.key === 'Enter' && (!node.multiline || event.ctrlKey || event.metaKey)) {
			event.preventDefault();
			commit();
		}
	}
</script>

{#if node.multiline}
	<textarea
		{id}
		class="cfg-input"
		{disabled}
		value={shown}
		placeholder={node.nullable ? 'not set' : ''}
		aria-invalid={error ? 'true' : undefined}
		aria-describedby={error ? `${id}-error` : undefined}
		oninput={(e) => (draft = e.currentTarget.value)}
		onblur={commit}
		{onkeydown}
	></textarea>
{:else}
	<input
		{id}
		class="cfg-input"
		class:cfg-input--number={numeric}
		type={numeric ? 'number' : 'text'}
		inputmode={numeric ? 'decimal' : undefined}
		step={numeric ? (node.kind === 'int' ? 1 : 'any') : undefined}
		min={numeric ? (node.minimum ?? undefined) : undefined}
		max={numeric ? (node.maximum ?? undefined) : undefined}
		spellcheck="false"
		autocomplete="off"
		{disabled}
		value={shown}
		placeholder={node.nullable ? 'not set' : ''}
		aria-invalid={error ? 'true' : undefined}
		aria-describedby={error ? `${id}-error` : undefined}
		oninput={(e) => {
			// A number box reports "" for text it cannot read; that must not
			// look like an emptied box, which means "not set".
			const el = e.currentTarget;
			draft = numeric && el.validity?.badInput ? 'NaN' : el.value;
		}}
		onblur={commit}
		{onkeydown}
	/>
{/if}
{#if error}
	<p id="{id}-error" class="cfg-error" role="alert">{error}</p>
{/if}
