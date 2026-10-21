<script lang="ts">
	// First-run wizard (TD-1101, TD-5172): welcome → Add a model → workspace →
	// done. "Add a model" walks server URL → key → pick a model → label →
	// test message. Every step is skippable-with-consequence (stated next to
	// the skip link); nothing is forced. All daemon contact rides the
	// onboarding store — this component renders state and forwards clicks.
	import {
		onboarding,
		WIZARD_STEPS,
		nextStep,
		backStep,
		closeWizard,
		setModelUrl,
		listModels,
		pickModel,
		addModel,
		testModel,
		backModelStep,
		isLocalUrl,
		chooseWorkspace,
		finish,
	} from '../onboarding.svelte.js';
	import { MODELS_DOC_URL, openDoc } from '../docs-links';

	let urlInput = $state('');
	let keyInput = $state('');
	let filter = $state('');
	let labelInput = $state('');
	let descriptionInput = $state('');

	const stepIndex = $derived(WIZARD_STEPS.indexOf(onboarding.step));
	const form = $derived(onboarding.model);
	const shown = $derived(
		form.models.filter((m) => m.id.toLowerCase().includes(filter.trim().toLowerCase())).slice(0, 200)
	);

	// The label fields start from the store's guess each time a model is picked.
	$effect(() => {
		if (form.sub === 'label') {
			labelInput = form.label;
			descriptionInput = form.description;
		}
	});

	function price(value: number | null | undefined): string {
		return value === null || value === undefined ? '' : `$${value.toFixed(2)}`;
	}

	async function pickFolder(): Promise<void> {
		const { open } = await import('@tauri-apps/plugin-dialog');
		const chosen = await open({ directory: true, multiple: false });
		if (typeof chosen === 'string' && chosen.length > 0) chooseWorkspace(chosen);
	}
</script>

{#if onboarding.open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div
		class="wizard-overlay"
		role="dialog"
		tabindex="-1"
		aria-modal="true"
		aria-label="First-run setup"
		onclick={(e) => {
			if (e.target === e.currentTarget) closeWizard();
		}}
	>
		<div class="wizard">
			<div class="dots" aria-hidden="true">
				{#each WIZARD_STEPS as _s, i}
					<span class="dot" class:dot--done={i <= stepIndex}></span>
				{/each}
			</div>

			{#if onboarding.step === 'welcome'}
				<h1 class="step-title">Welcome to TST Desk</h1>
				<p class="step-body">
					Two quick things: add a model for the agent to talk to, and pick the folder it works in.
					You can change either later from the model picker or Settings.
				</p>
				<div class="actions">
					<button class="btn btn--ghost" type="button" onclick={() => openDoc(MODELS_DOC_URL)}>
						How models work
					</button>
					<button class="btn btn--primary" type="button" onclick={nextStep}>Get started</button>
					<button class="btn btn--ghost" type="button" onclick={closeWizard}>Skip everything</button>
				</div>
			{:else if onboarding.step === 'model'}
				<h1 class="step-title">Add a model</h1>
				{#if form.sub === 'url'}
					<p class="step-body">
						Paste the API URL of any server that speaks the OpenAI chat API: a hosted provider, or a
						model server on this computer. It usually ends in <span class="mono">/v1</span>.
					</p>
					<label class="field">
						<span class="field-label">Server URL</span>
						<input
							class="field-input"
							type="url"
							autocomplete="off"
							placeholder="https://api.example.com/v1"
							bind:value={urlInput}
						/>
					</label>
					{#if form.listing}
						<p class="verdict verdict--pending">Asking the server for its models…</p>
					{:else if form.listError}
						<p class="verdict verdict--bad">{form.listError}</p>
					{/if}
					<div class="actions">
						<button class="btn btn--ghost" type="button" onclick={backModelStep}>Back</button>
						<button
							class="btn btn--primary"
							type="button"
							disabled={urlInput.trim().length === 0 || form.listing}
							onclick={() => setModelUrl(urlInput)}>Continue</button
						>
						<button class="btn btn--ghost" type="button" onclick={nextStep}>
							Skip — chats fail until a model is added
						</button>
					</div>
				{:else if form.sub === 'key'}
					<p class="step-body">
						Paste the API key from your provider's account page. It goes into the OS keychain, never
						a file, and is only sent to <span class="mono">{form.baseUrl}</span>.
					</p>
					<label class="field">
						<span class="field-label">API key</span>
						<input
							class="field-input"
							type="password"
							autocomplete="off"
							placeholder="sk-…"
							bind:value={keyInput}
						/>
					</label>
					{#if form.listing}
						<p class="verdict verdict--pending">Asking the server for its models…</p>
					{:else if form.listError}
						<p class="verdict verdict--bad">{form.listError}</p>
					{/if}
					<div class="actions">
						<button class="btn btn--ghost" type="button" onclick={backModelStep}>Back</button>
						<button
							class="btn btn--primary"
							type="button"
							disabled={keyInput.trim().length === 0 || form.listing}
							onclick={() => listModels(keyInput)}>List models</button
						>
					</div>
				{:else if form.sub === 'pick'}
					<p class="step-body">
						{form.models.length} model{form.models.length === 1 ? '' : 's'} at
						<span class="mono">{form.baseUrl}</span>. Pick the one the agent should use.
					</p>
					{#if form.models.length > 8}
						<input
							class="field-input"
							type="search"
							placeholder="Filter models"
							aria-label="Filter models"
							bind:value={filter}
						/>
					{/if}
					<div class="preset-list model-list" role="listbox" aria-label="Models">
						{#each shown as row (row.id)}
							<button
								class="preset"
								type="button"
								role="option"
								aria-selected={row.id === form.slug}
								onclick={() => pickModel(row)}
							>
								<span class="preset-name mono">{row.id}</span>
								<span class="preset-meta">
									{#if row.context_window}{Math.round(row.context_window / 1000)}k{/if}
									{#if row.input_price !== null && row.input_price !== undefined}
										· {price(row.input_price)} / {price(row.output_price)} per M
									{/if}
								</span>
							</button>
						{/each}
					</div>
					<div class="actions">
						<button class="btn btn--ghost" type="button" onclick={backModelStep}>Back</button>
					</div>
				{:else if form.sub === 'label'}
					<p class="step-body">
						Name it the way you want to see it in the model picker, and say in a few words what it is
						for. <span class="mono">{form.slug}</span>
					</p>
					<label class="field">
						<span class="field-label">Label</span>
						<input class="field-input field-input--text" maxlength="40" bind:value={labelInput} />
					</label>
					<label class="field">
						<span class="field-label">Description (optional)</span>
						<input
							class="field-input field-input--text"
							maxlength="120"
							placeholder="General coding and chat"
							bind:value={descriptionInput}
						/>
					</label>
					{#if form.addError}
						<p class="verdict verdict--bad">{form.addError}</p>
					{/if}
					<div class="actions">
						<button class="btn btn--ghost" type="button" onclick={backModelStep}>Back</button>
						<button
							class="btn btn--primary"
							type="button"
							disabled={labelInput.trim().length === 0 || form.adding}
							onclick={() => addModel(labelInput, descriptionInput)}
							>{form.adding ? 'Saving…' : 'Save model'}</button
						>
					</div>
				{:else}
					<p class="step-body">
						Saved as <strong>{form.label}</strong> and picked as the model for new chats. Sending it
						one short test message:
					</p>
					{#if form.testing}
						<p class="verdict verdict--pending">Waiting for a reply…</p>
					{:else if form.test}
						<p
							class="verdict"
							class:verdict--ok={form.test.ok}
							class:verdict--bad={!form.test.ok}
						>
							{form.test.ok ? `It replied: “${form.test.reply}”` : form.test.detail}
						</p>
					{/if}
					<div class="actions">
						{#if form.test && !form.test.ok}
							<button class="btn" type="button" onclick={testModel}>Try again</button>
						{/if}
						<button
							class="btn btn--primary"
							type="button"
							disabled={form.testing}
							onclick={() => (onboarding.addOnly ? finish() : nextStep())}
							>Continue</button
						>
					</div>
				{/if}
				{#if form.sub === 'url' && isLocalUrl(urlInput)}
					<p class="hint">A server on this computer needs no key.</p>
				{/if}
			{:else if onboarding.step === 'workspace'}
				<h1 class="step-title">Workspace</h1>
				<p class="step-body">
					Pick the folder the agent works in. It only writes inside the boundary you set.
				</p>
				<div class="actions">
					<button class="btn btn--ghost" type="button" onclick={backStep}>Back</button>
					<button class="btn btn--primary" type="button" onclick={pickFolder}>Choose folder…</button>
					<button class="btn btn--ghost" type="button" onclick={nextStep}>
						Skip — you can open one from the title bar later
					</button>
				</div>
			{:else}
				<h1 class="step-title">You're set</h1>
				<p class="step-body">
					The session is live — type in the chat on the left and the agent runs in the
					workspace you picked.
				</p>
				<div class="actions">
					<button class="btn btn--primary" type="button" onclick={finish}>Start working</button>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.wizard-overlay {
		position: fixed;
		inset: 0;
		background: color-mix(in srgb, var(--color-ground) 78%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: var(--z-modal);
	}

	.wizard {
		width: min(34rem, calc(100vw - var(--space-8)));
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-xl);
		box-shadow: var(--shadow-lg);
		padding: var(--space-6);
		display: flex;
		flex-direction: column;
		gap: var(--space-4);
	}

	.dots {
		display: flex;
		gap: var(--space-2);
	}

	.dot {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-full);
		background: var(--color-hairline);
	}

	.dot--done {
		background: var(--color-accent);
	}

	.step-title {
		margin: 0;
		font-family: var(--font-display);
		font-size: var(--text-xl);
		font-weight: var(--weight-medium);
		letter-spacing: var(--tracking-display);
		color: var(--color-ink);
	}

	.step-body {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-ink-secondary);
		line-height: 1.5;
	}

	.mono {
		font-family: var(--font-mono);
		font-size: var(--text-xs);
		user-select: all;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
	}

	.field-label {
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
	}

	.field-input {
		font-size: var(--text-sm);
		font-family: var(--font-mono);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
		color: var(--color-ink);
	}

	.verdict {
		margin: 0;
		font-size: var(--text-xs);
		line-height: 1.4;
		color: var(--color-ink-secondary);
	}

	.verdict--ok {
		color: var(--color-ok);
	}

	.verdict--bad {
		color: var(--color-err);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		justify-content: flex-end;
	}

	.btn {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		padding: var(--space-2) var(--space-3);
		border-radius: var(--radius-md);
		border: var(--border-width) solid var(--color-hairline);
		background: var(--color-ground);
		color: var(--color-ink);
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.btn--primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-on-accent);
	}

	.btn--primary:hover:not(:disabled) {
		background: var(--color-accent-hover);
	}

	.btn--ghost {
		border-color: transparent;
		background: transparent;
		color: var(--color-ink-secondary);
	}

	.preset-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-2);
	}

	.preset {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--space-3);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		background: var(--color-ground);
		cursor: pointer;
		text-align: left;
	}

	.preset[aria-selected='true'] {
		border-color: var(--color-accent);
	}

	.model-list {
		max-height: 16rem;
		overflow-y: auto;
	}

	.preset-meta {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		white-space: nowrap;
	}

	.field-input--text {
		font-family: var(--font-sans);
	}

	.hint {
		margin: 0;
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.preset-name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-ink);
	}

</style>
