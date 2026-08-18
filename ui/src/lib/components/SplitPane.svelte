<script lang="ts">
	// Presentational two-pane split with a draggable, keyboard-operable divider.
	// Layout-only: positions two children side by side. Divider persistence is
	// this component's concern; the math lives in ../splitpane for testability.
	//
	// Uses design tokens (--space-1) exclusively — no hardcoded spacing.
	// The left pane width is a percentage of the container.
	import {
		DEFAULT_LEFT_PCT,
		DIVIDER_HIT_MIN_PX,
		KEYBOARD_STEP_PCT,
		MAX_LEFT_PCT,
		MIN_LEFT_PCT,
		attachDragListeners,
		clampLeftPct,
		pxToLeftPct,
		readPersistedLeftPct,
		writePersistedLeftPct
	} from '../splitpane';

	interface Props {
		left: import('svelte').Snippet;
		right: import('svelte').Snippet;
	}

	let { left, right }: Props = $props();

	// Percentage of the container width occupied by the left pane.
	let leftPct = $state(DEFAULT_LEFT_PCT);
	// true while the user is actively dragging.
	let dragging = $state(false);

	let container: HTMLDivElement | undefined = $state();
	// Window listeners are the drag mechanism; pointer capture is only
	// an optimisation. WKWebView does not always honour capture, and
	// binding move/up to the 4px divider then latches `dragging` (TD-1011).
	let detachDrag: (() => void) | null = null;

	function persist() {
		writePersistedLeftPct(window.localStorage, leftPct);
	}

	function onDividerDown(e: PointerEvent) {
		if (dragging) return;
		dragging = true;
		(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
		detachDrag = attachDragListeners(window, onDividerMove, onDividerUp);
	}

	function onDividerMove(e: PointerEvent) {
		if (!dragging) return;
		const rect = container?.getBoundingClientRect();
		const next = pxToLeftPct(e.clientX - (rect?.left ?? 0), rect?.width ?? 0);
		if (next !== null) leftPct = next;
	}

	function onDividerUp() {
		if (!dragging) return;
		dragging = false;
		detachDrag?.();
		detachDrag = null;
		// Persist once the drag settles.
		persist();
	}

	function onDividerKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowLeft') {
			leftPct = clampLeftPct(leftPct - KEYBOARD_STEP_PCT);
		} else if (e.key === 'ArrowRight') {
			leftPct = clampLeftPct(leftPct + KEYBOARD_STEP_PCT);
		} else {
			return;
		}
		e.preventDefault();
		persist();
	}

	// Restore the persisted divider position on mount. $effect runs
	// client-side only, so localStorage is safe to touch here.
	$effect(() => {
		leftPct = readPersistedLeftPct(window.localStorage);
	});

	// A drag in flight when the pane unmounts must not leave window
	// listeners behind.
	$effect(() => {
		return () => {
			detachDrag?.();
			detachDrag = null;
		};
	});
</script>

<div
	bind:this={container}
	class="splitpane"
	class:dragging
	data-dragging={dragging ? "true" : "false"}
	data-left-pct={leftPct}
	style={`grid-template-columns: ${leftPct}% var(--space-1) 1fr; --divider-hit: ${DIVIDER_HIT_MIN_PX}px;`}
>
	<div class="pane left">{@render left()}</div>
	<!-- ARIA window-splitter pattern: a focusable, keyboard-operable
	     separator is the canonical role for a movable pane divider, so the
	     generic noninteractive-element a11y rules do not apply here. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
	<div
		class="divider"
		role="separator"
		tabindex="0"
		aria-orientation="vertical"
		aria-valuenow={Math.round(leftPct)}
		aria-valuemin={MIN_LEFT_PCT}
		aria-valuemax={MAX_LEFT_PCT}
		onpointerdown={onDividerDown}
		onkeydown={onDividerKeydown}
	></div>
	<div class="pane right">{@render right()}</div>
</div>

<style>
	.splitpane {
		display: grid;
		/* Fill the parent flex line; otherwise the grid shrink-wraps and
		   percentage columns resolve against an indefinite width. */
		width: 100%;
		height: 100%;
		overflow: hidden;
	}

	.pane {
		min-width: 0;
		height: 100%;
		overflow: auto;
	}

	.divider {
		position: relative;
		width: 100%;
		height: 100%;
		cursor: col-resize;
		background: var(--color-border);
		transition: background var(--transition-fast);
		touch-action: none;
	}

	/* Painted rule stays the grid track (`--space-1` = 4px). The hit
	   target is at least 8px without widening the line (TD-1011). */
	.divider::before {
		content: "";
		position: absolute;
		top: 0;
		bottom: 0;
		left: 50%;
		width: var(--divider-hit);
		transform: translateX(-50%);
	}

	.divider:hover,
	.divider:focus-visible,
	.splitpane.dragging .divider {
		background: var(--color-accent);
	}
</style>
