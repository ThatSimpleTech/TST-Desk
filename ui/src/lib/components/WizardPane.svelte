<script lang="ts">
	// First-run wizard (TD-1101): welcome → API key → preset → workspace → done.
	// Every step is skippable-with-consequence (the consequence is stated next
	// to the skip link); nothing is forced. All daemon contact rides the
	// onboarding store — this component renders state and forwards clicks.
	import {
		onboarding,
		WIZARD_STEPS,
		nextStep,
		backStep,
		closeWizard,
		storeKey,
		validateKey,
		removeKey,
		choosePreset,
		chooseWorkspace,
		finish,
	} from '../onboarding.svelte.js';
	import Icon from './Icon.svelte';

	let keyInput = $state('');

	const stepIndex = $derived(WIZARD_STEPS.indexOf(onboarding.step));

	async function pickFolder(): Promise<void> {
		const { open } = await import('@tauri-apps/plugin-dialog');
		const chosen = await open({ directory: true, multiple: false });
		if (typeof chosen === 'string' && chosen.length > 0) chooseWorkspace(chosen);
	}
</script>

{#if onboarding.open}
	<div class="wizard-overlay" role="dialog" aria-modal="true" aria-label="First-run setup">
		<div class="wizard">
			<div class="dots" aria-hidden="true">
				{#each WIZARD_STEPS as _s, i}
					<span class="dot" class:dot--done={i <= stepIndex}></span>
				{/each}
			</div>

			{#if onboarding.step === 'welcome'}
				<h1 class="step-title">Welcome to TST Desk</h1>
				<p class="step-body">
					Four quick things: an API key, a model preset, and a workspace folder. Each step is
					skippable — you can change any of it later from the gear in the title bar.
				</p>
				<div class="actions">
					<button class="btn btn--primary" type="button" onclick={nextStep}>Get started</button>
					<button class="btn btn--ghost" type="button" onclick={closeWizard}>Skip everything</button>
				</div>
			{:else if onboarding.step === 'key'}
				<h1 class="step-title">Provider API key</h1>
				<p class="step-body">
					Keys are stored in the OS keychain — never in a file. Get one at
					<span class="mono">openrouter.ai/keys</span>.
				</p>
				<label class="field">
					<span class="field-label">API key</span>
					<input
						class="field-input"
						type="password"
						autocomplete="off"
						placeholder="sk-or-…"
						bind:value={keyInput}
					/>
				</label>
				{#if onboarding.validating}
					<p class="verdict verdict--pending">Checking the key with the provider…</p>
				{:else if onboarding.validation}
					<p class="verdict" class:verdict--ok={onboarding.validation.ok} class:verdict--bad={!onboarding.validation.ok}>
						{onboarding.validation.detail}
					</p>
				{/if}
				<div class="actions">
					<button class="btn btn--ghost" type="button" onclick={backStep}>Back</button>
					<button
						class="btn"
						type="button"
						disabled={keyInput.trim().length === 0 || onboarding.validating}
						onclick={() => storeKey(keyInput)}
					>
						{onboarding.hasApiKey ? 'Key stored' : 'Store key'}
					</button>
					<button
						class="btn"
						type="button"
						disabled={!onboarding.hasApiKey || onboarding.validating}
						onclick={validateKey}>Validate</button
					>
					{#if onboarding.hasApiKey}
						<button class="btn btn--ghost" type="button" onclick={removeKey}>
							Remove stored key
						</button>
					{/if}
					<button class="btn btn--primary" type="button" onclick={nextStep}>Continue</button>
					<button class="btn btn--ghost" type="button" onclick={nextStep}>
						Skip — chats fail until a key is stored
					</button>
				</div>
			{:else if onboarding.step === 'preset'}
				<h1 class="step-title">Model preset</h1>
				<p class="step-body">
					A preset picks the models each tier routes to. You can edit any of it in the config
					later — nothing here is locked in.
				</p>
				<div class="preset-list" role="radiogroup" aria-label="Model preset">
					{#each onboarding.presets as name}
						<button
							class="preset"
							class:preset--active={name === onboarding.activePreset}
							type="button"
							role="radio"
							aria-checked={name === onboarding.activePreset}
							onclick={() => choosePreset(name)}
						>
							<span class="preset-name">{name}</span>
							{#if name === onboarding.activePreset}
								<span class="preset-check"><Icon name="check" size={14} /></span>
							{/if}
						</button>
					{/each}
				</div>
				<div class="actions">
					<button class="btn btn--ghost" type="button" onclick={backStep}>Back</button>
					<button class="btn btn--primary" type="button" onclick={nextStep}>Continue</button>
				</div>
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
		background: color-mix(in srgb, var(--color-bg) 78%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 90;
	}

	.wizard {
		width: min(34rem, calc(100vw - var(--space-8)));
		background: var(--color-bg-raised);
		border: var(--border-width) solid var(--color-border);
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
		background: var(--color-border);
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
		color: var(--color-text);
	}

	.step-body {
		margin: 0;
		font-size: var(--text-sm);
		color: var(--color-text-secondary);
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
		color: var(--color-text-secondary);
	}

	.field-input {
		font-size: var(--text-sm);
		font-family: var(--font-mono);
		padding: var(--space-2) var(--space-3);
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		background: var(--color-bg);
		color: var(--color-text);
	}

	.verdict {
		margin: 0;
		font-size: var(--text-xs);
		line-height: 1.4;
		color: var(--color-text-secondary);
	}

	.verdict--ok {
		color: var(--color-success);
	}

	.verdict--bad {
		color: var(--color-danger);
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
		border: var(--border-width) solid var(--color-border);
		background: var(--color-bg);
		color: var(--color-text);
		cursor: pointer;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}

	.btn--primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-accent-text);
	}

	.btn--primary:hover:not(:disabled) {
		background: var(--color-accent-hover);
	}

	.btn--ghost {
		border-color: transparent;
		background: transparent;
		color: var(--color-text-secondary);
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
		border: var(--border-width) solid var(--color-border);
		border-radius: var(--radius-md);
		background: var(--color-bg);
		cursor: pointer;
		text-align: left;
	}

	.preset--active {
		border-color: var(--color-accent);
	}

	.preset-name {
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-text);
	}

	.preset-check {
		display: inline-flex;
		color: var(--color-accent);
	}
</style>
