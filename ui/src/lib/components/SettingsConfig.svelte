<script lang="ts">
	// Settings → Configuration (TD-5203): every config.yaml key, drawn from
	// the schema the daemon reads off its config models. Each key saves on
	// its own; the daemon checks the whole file before writing it and keeps
	// its comments. Only this computer's own window may use it.
	import { onMount } from 'svelte';
	import {
		configEditor,
		loadConfigEditor,
		startConfigEditor,
	} from '../config-editor.svelte.js';
	import { pathKey, visibleKeys } from '../config-editor.js';
	import ConfigSection from './config/ConfigSection.svelte';
	import { restoreLastGoodConfig, settings } from '../settings.svelte.js';
	import './config/config-editor.css';

	onMount(() => {
		startConfigEditor();
		loadConfigEditor();
	});

	const root = $derived(configEditor.defs[configEditor.root]);
	const visible = $derived(
		visibleKeys(configEditor.query, configEditor.root, configEditor.defs, configEditor.values),
	);
	const shownSections = $derived(
		(root?.fields ?? []).filter((f) => visible === null || visible.has(pathKey([f.name]))),
	);
</script>

<div class="cfg-root">
	{#if settings.configError}
		<div class="cfg-broken" role="alert">
			<p class="cfg-error">config.yaml did not load when the app started: {settings.configError}</p>
			{#if settings.configRunningOn === 'last_good'}
				<p class="cfg-hint">
					The app is running on the last copy of config.yaml that worked. Restore puts that
					copy back and keeps the broken file beside it as config.yaml.damaged-….
				</p>
				<button class="cfg-restore" type="button" onclick={restoreLastGoodConfig}
					>Restore the last working copy</button
				>
			{:else}
				<p class="cfg-hint">
					There is no earlier working copy, so the app is running on the shipped defaults.
					Fix config.yaml by hand; the next start uses it once it loads.
				</p>
			{/if}
		</div>
	{/if}
	<p class="cfg-hint">
		Every key in config.yaml. A change saves as you make it and applies to new sessions; the
		comments in the file stay as they are. Reset puts back what TST Desk ships.
	</p>
	{#if configEditor.status === 'computer_only'}
		<p class="cfg-note" role="status">
			Configuration can be read and changed only on the computer itself. Open Settings →
			Configuration there.
		</p>
	{:else if configEditor.status === 'error'}
		<p class="cfg-error" role="alert">{configEditor.error}</p>
		<p class="cfg-hint">Fix config.yaml by hand, then reopen this page.</p>
	{:else if !root}
		<p class="cfg-muted" role="status">Loading the configuration…</p>
	{:else}
		<div class="cfg-searchbar">
			<input
				class="cfg-search"
				type="search"
				placeholder="Search keys and descriptions"
				aria-label="Search configuration"
				bind:value={configEditor.query}
				onkeydown={(e) => {
					// Escape clears a search first; with none, it closes Settings as usual.
					if (e.key !== 'Escape' || configEditor.query === '') return;
					e.stopPropagation();
					e.preventDefault();
					configEditor.query = '';
				}}
			/>
		</div>
		{#if configEditor.file}
			<p class="cfg-muted">Editing <span class="cfg-key">{configEditor.file}</span></p>
		{/if}
		{#each shownSections as field (field.name)}
			<ConfigSection {field} {visible} />
		{:else}
			<p class="cfg-muted">No key matches “{configEditor.query}”.</p>
		{/each}
	{/if}
</div>
