<script lang="ts">
	// The model picker, built like Claude's: a short model list with a check
	// on the current one, an "Effort" row that opens a submenu (Low to Max,
	// Ultracode last), a "More models" row for the raw routes, and "Teams"
	// (a model per role) only when the config has one. The chip reads
	// "<Model> <Effort>", e.g. "House Medium". A model is a catalog preset.
	//
	// TD-5172: every row has one job a person can look up. Models show their
	// label with the config's one-line description under it; unlabelled
	// presets stay out of the list (unless current). "Add a model" opens the
	// walkthrough and "How models work" opens docs/models.md. With no model
	// yet, the chip itself reads "Add a model".
	import {
		chipLabel,
		EFFORT_HINT,
		EFFORT_OPTIONS,
		effortLabel,
		groupModels,
		modelDescription,
		modelLabel,
	} from "../../effort";
	import { MODELS_DOC_URL, openDoc } from "../../docs-links";
	import { openAddModel } from "../../onboarding.svelte.js";
	import { session, setEffort, setFast, setSessionPreset } from "../../session-status.svelte.js";
	import { settings } from "../../settings.svelte.js";

	type View = "main" | "effort" | "more" | "teams";

	let open = $state(false);
	let view = $state<View>("main");

	const current = $derived(session.preset);
	const groups = $derived(
		groupModels(settings.presets, {
			more: settings.morePresets,
			teams: settings.teamPresets,
			labels: settings.presetLabels,
			current,
		}),
	);
	const locked = $derived(session.turnActive);

	function close(): void {
		open = false;
		view = "main";
	}

	function toggle(): void {
		if (!settings.hasModel) {
			openAddModel();
			return;
		}
		if (open) close();
		else open = true;
	}

	function addModel(): void {
		close();
		openAddModel();
	}

	function howModelsWork(): void {
		close();
		void openDoc(MODELS_DOC_URL);
	}

	function pickModel(name: string): void {
		if (name !== current) setSessionPreset(name);
		close();
	}

	// Close on a click outside or Escape (Escape steps back from a submenu first).
	$effect(() => {
		if (!open) return;
		const onPointerDown = (event: PointerEvent): void => {
			const inside = event
				.composedPath()
				.some((el) => el instanceof HTMLElement && el.classList.contains("picker"));
			if (!inside) close();
		};
		const onKey = (event: KeyboardEvent): void => {
			if (event.key !== "Escape") return;
			if (view === "main") close();
			else view = "main";
		};
		document.addEventListener("pointerdown", onPointerDown);
		document.addEventListener("keydown", onKey);
		return () => {
			document.removeEventListener("pointerdown", onPointerDown);
			document.removeEventListener("keydown", onKey);
		};
	});
</script>

<span class="picker">
	<button
		class="chip"
		type="button"
		aria-label="Model and effort"
		aria-haspopup="menu"
		aria-expanded={open}
		onclick={toggle}
	>
		{settings.hasModel ? chipLabel(current, session.effort, settings.presetLabels) : "Add a model"}{#if settings.hasModel && session.fast}<span class="fast-tag">{" · Fast"}</span>{/if}
		<span class="caret" aria-hidden="true"></span>
	</button>

	{#if open}
		<div class="menu" role="menu" aria-label="Model and effort">
			{#if view === "main"}
				{#each groups.main as name (name)}
					<button
						class="item"
						type="button"
						role="menuitemradio"
						aria-checked={name === current}
						disabled={locked && name !== current}
						title={locked && name !== current ? "Cannot change model while a turn is running" : ""}
						onclick={() => pickModel(name)}
					>
						<span class="text">
							<span class="label">{modelLabel(name, settings.presetLabels)}</span>
							{#if modelDescription(name, settings.presetDescriptions)}
								<span class="sub">{modelDescription(name, settings.presetDescriptions)}</span>
							{/if}
						</span>
						<span class="check" aria-hidden="true">{name === current ? "✓" : ""}</span>
					</button>
				{/each}
				<div class="rule" role="separator"></div>
				<button class="item" type="button" role="menuitem" onclick={() => (view = "effort")}>
					<span class="label">Effort</span>
					<span class="value">{effortLabel(session.effort)}</span>
					<span class="more" aria-hidden="true">›</span>
				</button>
				{#if session.fastAvailable}
					<button
						class="item"
						type="button"
						role="menuitemcheckbox"
						aria-checked={session.fast}
						onclick={() => setFast(!session.fast)}
					>
						<span class="text">
							<span class="label">Fast</span>
							{#if session.fastNote}
								<span class="sub">{session.fastNote}</span>
							{/if}
						</span>
						<span class="value">{session.fast ? "On" : "Off"}</span>
					</button>
				{/if}
				{#if groups.more.length > 0}
					<button class="item" type="button" role="menuitem" onclick={() => (view = "more")}>
						<span class="label">More models</span>
						<span class="more" aria-hidden="true">›</span>
					</button>
				{/if}
				{#if groups.teams.length > 0}
					<button
						class="item"
						type="button"
						role="menuitem"
						title="A team gives the planning, doing and checking roles their own models."
						onclick={() => (view = "teams")}
					>
						<span class="label">Teams</span>
						<span class="more" aria-hidden="true">›</span>
					</button>
				{/if}
				<div class="rule" role="separator"></div>
				<button class="item" type="button" role="menuitem" onclick={addModel}>
					<span class="label">Add a model</span>
				</button>
				<button class="item item--link" type="button" role="menuitem" onclick={howModelsWork}>
					<span class="label">How models work</span>
				</button>
			{:else if view === "effort"}
				<button class="back" type="button" onclick={() => (view = "main")}>‹ Effort</button>
				<p class="hint">{EFFORT_HINT}</p>
				{#each EFFORT_OPTIONS as option (option.id)}
					<button
						class="item"
						class:item--accent={option.accent}
						type="button"
						role="menuitemradio"
						aria-checked={option.id === session.effort}
						onclick={() => {
							setEffort(option.id);
							close();
						}}
					>
						<span class="label">{option.label}</span>
						{#if option.badge}<span class="badge">{option.badge}</span>{/if}
						{#if option.warning}<span class="badge badge--warn">{option.warning}</span>{/if}
						<span class="check" aria-hidden="true">{option.id === session.effort ? "✓" : ""}</span>
					</button>
				{/each}
			{:else}
				<button class="back" type="button" onclick={() => (view = "main")}>
					‹ {view === "teams" ? "Teams" : "More models"}
				</button>
				{#if view === "teams"}
					<p class="hint">Each team gives planning, doing and checking their own models.</p>
				{/if}
				{#each view === "teams" ? groups.teams : groups.more as name (name)}
					<button
						class="item"
						type="button"
						role="menuitemradio"
						aria-checked={name === current}
						disabled={locked && name !== current}
						onclick={() => pickModel(name)}
					>
						<span class="text">
							<span class="label">{modelLabel(name, settings.presetLabels)}</span>
							{#if modelDescription(name, settings.presetDescriptions)}
								<span class="sub">{modelDescription(name, settings.presetDescriptions)}</span>
							{/if}
						</span>
						<span class="check" aria-hidden="true">{name === current ? "✓" : ""}</span>
					</button>
				{/each}
			{/if}
		</div>
	{/if}
</span>

<style>
	.picker {
		position: relative;
		display: inline-flex;
		min-width: 0;
		max-width: 100%;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		max-width: 100%;
		font-family: var(--font-sans);
		font-size: var(--text-xs);
		font-weight: var(--weight-medium);
		color: var(--color-ink-secondary);
		background: transparent;
		border: none;
		border-radius: var(--radius-full);
		padding: var(--space-1) var(--space-2);
		cursor: pointer;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.chip:hover {
		background: var(--color-sunken);
		color: var(--color-ink);
	}

	/* Fast on (TD-5176): the chip says so, in the accent the effort menu uses for Ultracode. */
	.fast-tag {
		color: var(--color-accent);
		font-weight: var(--weight-medium);
	}

	.caret {
		width: 5px;
		height: 5px;
		border-right: 1.5px solid var(--color-ink-muted);
		border-bottom: 1.5px solid var(--color-ink-muted);
		transform: translateY(-1px) rotate(45deg);
	}

	.menu {
		position: absolute;
		bottom: calc(100% + var(--space-1));
		left: 0;
		z-index: 10;
		display: flex;
		flex-direction: column;
		min-width: 15rem;
		max-width: min(22rem, 90vw);
		padding: var(--space-1);
		background: var(--color-lifted);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-md);
	}

	.item,
	.back {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		padding: var(--space-1) var(--space-2);
		font-family: var(--font-sans);
		font-size: var(--text-sm);
		text-align: left;
		color: var(--color-ink);
		background: transparent;
		border: none;
		border-radius: var(--radius-sm);
		cursor: pointer;
	}

	.item:hover:not(:disabled),
	.back:hover {
		background: var(--color-sunken);
	}

	.item:focus-visible,
	.back:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: -1px;
	}

	.item:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.item--accent .label {
		color: var(--color-accent);
		font-weight: var(--weight-medium);
	}

	.back {
		color: var(--color-ink-secondary);
		font-size: var(--text-xs);
	}

	.label {
		flex: 1;
		min-width: 0;
	}

	.text {
		display: flex;
		flex: 1;
		flex-direction: column;
		min-width: 0;
	}

	.sub {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.item--link .label {
		color: var(--color-ink-secondary);
	}

	.value,
	.more {
		color: var(--color-ink-muted);
		font-size: var(--text-xs);
	}

	.check {
		width: 1em;
		text-align: center;
		color: var(--color-ink);
	}

	.hint {
		margin: 0;
		padding: var(--space-1) var(--space-2) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
	}

	.badge {
		font-size: var(--text-xs);
		color: var(--color-ink-muted);
		background: var(--color-sunken);
		border-radius: var(--radius-full);
		padding: 0 var(--space-2);
		white-space: nowrap;
	}

	.badge--warn {
		color: var(--color-warn);
	}

	.rule {
		height: 1px;
		margin: var(--space-1) 0;
		background: var(--color-hairline);
	}
</style>
