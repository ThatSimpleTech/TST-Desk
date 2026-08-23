<script lang="ts">
	// Named API keys (TD-1717). Presence and given names only — the secret
	// never enters the settings store.
	import {
		settings,
		storeNamedKey,
		renameCredential,
		saveCredentialUrl,
		deleteNamedKey,
		validateNamedKey,
	} from "../settings.svelte.js";
	import { onboarding } from "../onboarding.svelte.js";

	let newName = $state("");
	let newUrl = $state("");
	let newKey = $state("");
	let drafts = $state<Record<string, string>>({});
	let urlDrafts = $state<Record<string, string>>({});

	function addKey(): void {
		if (newName.trim() === "" || newUrl.trim() === "" || newKey.trim() === "") return;
		storeNamedKey(newName, newKey, undefined, newUrl);
		newName = "";
		newUrl = "";
		newKey = "";
	}

	function commitName(id: string): void {
		const next = (drafts[id] ?? "").trim();
		const current = settings.credentials.find((c) => c.id === id)?.name;
		if (next === "" || next === current) return;
		renameCredential(id, next);
	}

	function commitUrl(id: string): void {
		const cred = settings.credentials.find((c) => c.id === id);
		if (cred === undefined) return;
		const next = (urlDrafts[id] ?? cred.base_url).trim();
		if (next === cred.base_url) return;
		saveCredentialUrl(id, drafts[id] ?? cred.name, next);
	}
</script>

{#if !settings.keyRequired}
	<p class="hint">
		The active preset runs on a local endpoint, so no key is sent unless a tier is bound to one
		below.
	</p>
{/if}

{#each settings.credentials as cred (cred.id)}
	<div class="row">
		<label class="field">
			<span class="field-name">Name</span>
			<input
				class="input"
				type="text"
				value={drafts[cred.id] ?? cred.name}
				oninput={(e) => (drafts[cred.id] = e.currentTarget.value)}
				onblur={() => commitName(cred.id)}
			/>
		</label>
		<label class="field">
			<span class="field-name">URL</span>
			<input
				class="input"
				type="url"
				value={urlDrafts[cred.id] ?? cred.base_url}
				placeholder="https://…/v1"
				oninput={(e) => (urlDrafts[cred.id] = e.currentTarget.value)}
				onblur={() => commitUrl(cred.id)}
			/>
		</label>
		<p class="status">{cred.stored ? "Stored in the OS keychain." : "No key stored."}</p>
		<div class="actions">
			<button
				class="btn"
				type="button"
				disabled={onboarding.validating || !cred.stored}
				onclick={() => validateNamedKey(cred.id)}>Test</button
			>
			<button class="btn btn--danger" type="button" onclick={() => deleteNamedKey(cred.id)}
				>Remove</button
			>
		</div>
	</div>
{/each}

<p class="hint">
	Add another key — the name is what Model settings will show. Any model bound to this key
	sends requests to this URL.
</p>
<label class="field">
	<span class="field-name">Name</span>
	<input class="input" type="text" bind:value={newName} placeholder="OpenRouter, Local, …" />
</label>
<label class="field">
	<span class="field-name">URL</span>
	<input
		class="input"
		type="url"
		bind:value={newUrl}
		placeholder="https://openrouter.ai/api/v1"
	/>
</label>
<label class="field">
	<span class="field-name">Key</span>
	<input
		class="input"
		type="password"
		autocomplete="off"
		bind:value={newKey}
		placeholder="sk-…"
	/>
</label>
<div class="actions">
	<button
		class="btn"
		type="button"
		disabled={newName.trim() === "" || newUrl.trim() === "" || newKey.trim() === ""}
		onclick={addKey}>Save key</button
	>
</div>
{#if onboarding.validation}
	<p class="hint" aria-live="polite">{onboarding.validation.detail}</p>
{/if}

<style>
	.hint {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-3) 0 0;
	}

	.row {
		margin-top: var(--space-4);
		padding-top: var(--space-3);
		border-top: 1px solid var(--color-hairline);
	}

	.row:first-of-type {
		border-top: 0;
		padding-top: 0;
	}

	.status {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		margin: var(--space-2) 0 0;
	}

	.field {
		display: grid;
		grid-template-columns: 6rem 1fr;
		align-items: center;
		gap: var(--space-3);
		margin-top: var(--space-3);
	}

	.field-name {
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
	}

	.input {
		font-family: var(--font-mono);
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-ground);
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-2);
	}

	.actions {
		display: flex;
		gap: var(--space-2);
		margin-top: var(--space-3);
	}

	.btn {
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: transparent;
		border: 1px solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-1) var(--space-3);
		cursor: pointer;
	}

	.btn:hover:not(:disabled) {
		border-color: var(--color-accent);
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.btn--danger:hover:not(:disabled) {
		border-color: var(--color-err);
		color: var(--color-err);
	}
</style>
