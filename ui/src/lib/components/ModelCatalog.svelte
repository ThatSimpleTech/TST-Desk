<script lang="ts">
	// TD-5237: every model in the catalog, with Duplicate, Rename and Remove.
	// Before this the walkthrough could only add one, so trying a variation
	// meant overwriting the entry already there. The daemon owns the catalog:
	// each button sends one message and the list follows its setup_state.
	import { onMount } from "svelte";
	import { onEvent, sendToDaemon } from "../connection-status.svelte.js";
	import { modelDescription, modelLabel } from "../effort";
	import { copyLabel, LABEL_MAX, renameTarget } from "../model-catalog";
	import { settings } from "../settings.svelte.js";

	let busy = $state<string | null>(null);
	let problem = $state<string | null>(null);
	let renaming = $state<string | null>(null);
	let draft = $state("");
	let confirming = $state<string | null>(null);

	// setup_state is the ack for all three; an error with no session is the
	// refusal, and its message says why (e.g. another model reuses this one).
	onMount(() =>
		onEvent((event) => {
			if (busy === null) return;
			if (event.type === "setup_state") {
				busy = null;
				problem = null;
			} else if (event.type === "error" && (event.session_id ?? null) === null) {
				busy = null;
				problem = event.message;
			}
		}),
	);

	function label(name: string): string {
		return modelLabel(name, settings.presetLabels);
	}

	function send(name: string, sent: boolean): void {
		problem = null;
		busy = sent ? name : null;
	}

	function duplicate(name: string): void {
		const taken = settings.presets.map(label);
		send(
			name,
			sendToDaemon({ type: "duplicate_preset", source: name, label: copyLabel(label(name), taken) }),
		);
	}

	function startRename(name: string): void {
		confirming = null;
		renaming = name;
		draft = label(name);
	}

	function commitRename(name: string): void {
		const next = renameTarget(draft, label(name));
		renaming = null;
		if (next === null) return;
		send(name, sendToDaemon({ type: "set_preset_label", name, label: next }));
	}

	function remove(name: string): void {
		confirming = null;
		send(name, sendToDaemon({ type: "delete_preset", name }));
	}
</script>

{#if settings.presets.length > 0}
	<h3 class="heading">What you have added</h3>
	<p class="hint">
		Duplicate one to try a variation without changing the original. Removing one deletes no key,
		and a chat already using it keeps it until you switch that chat.
	</p>
	<ul class="list" aria-label="Your models">
		{#each settings.presets as name (name)}
			<li class="row">
				{#if renaming === name}
					<input
						class="input"
						type="text"
						aria-label={`New name for ${label(name)}`}
						maxlength={LABEL_MAX}
						bind:value={draft}
						onkeydown={(e) => {
							if (e.key === "Enter") commitRename(name);
							if (e.key === "Escape") {
								e.stopPropagation();
								renaming = null;
							}
						}}
					/>
					<div class="actions">
						<button
							class="btn"
							type="button"
							aria-label={`Save name for ${label(name)}`}
							disabled={renameTarget(draft, label(name)) === null}
							onclick={() => commitRename(name)}>Save</button
						>
						<button
							class="btn"
							type="button"
							aria-label={`Cancel renaming ${label(name)}`}
							onclick={() => (renaming = null)}>Cancel</button
						>
					</div>
				{:else}
					<div class="text">
						<span class="name">{label(name)}</span>
						{#if name === settings.activePreset}
							<span class="tag">New chats use this</span>
						{/if}
						{#if modelDescription(name, settings.presetDescriptions)}
							<span class="sub">{modelDescription(name, settings.presetDescriptions)}</span>
						{/if}
					</div>
					{#if confirming === name}
						<div class="actions">
							<span class="ask">Remove {label(name)}?</span>
							<button
								class="btn btn--danger"
								type="button"
								aria-label={`Yes, remove ${label(name)}`}
								onclick={() => remove(name)}>Remove</button
							>
							<button
								class="btn"
								type="button"
								aria-label={`Keep ${label(name)}`}
								onclick={() => (confirming = null)}>Keep</button
							>
						</div>
					{:else}
						<div class="actions">
							<button
								class="btn"
								type="button"
								aria-label={`Duplicate ${label(name)}`}
								disabled={busy !== null}
								onclick={() => duplicate(name)}>Duplicate</button
							>
							<button
								class="btn"
								type="button"
								aria-label={`Rename ${label(name)}`}
								disabled={busy !== null}
								onclick={() => startRename(name)}>Rename</button
							>
							<button
								class="btn btn--danger"
								type="button"
								aria-label={`Remove ${label(name)}`}
								disabled={busy !== null}
								onclick={() => (confirming = name)}>Remove</button
							>
						</div>
					{/if}
				{/if}
			</li>
		{/each}
	</ul>
	{#if problem}
		<p class="hint problem" role="alert">{problem}</p>
	{/if}
{/if}

<style>
	.heading {
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

	.problem {
		color: var(--color-err);
	}

	.list {
		list-style: none;
		margin: var(--space-3) 0 0;
		padding: 0;
	}

	.row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2) var(--space-3);
		padding: var(--space-2) 0;
		border-top: var(--border-width) solid var(--color-hairline);
	}

	.text {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		min-width: 0;
	}

	.name {
		font-size: var(--text-sm);
		color: var(--color-ink);
	}

	.sub,
	.tag,
	.ask {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.ask {
		align-self: center;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
	}

	.input {
		flex: 1;
		min-width: 12rem;
		font-size: var(--text-sm);
		color: var(--color-ink);
		background: var(--color-ground);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-sm);
		padding: var(--space-2);
	}

	.btn {
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

	.btn--danger:hover:not(:disabled) {
		border-color: var(--color-err);
		color: var(--color-err);
	}
</style>
