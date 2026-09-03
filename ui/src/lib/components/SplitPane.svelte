<script lang="ts">
	// Presentational two-pane split with a draggable, keyboard-operable divider.
	// Layout-only: positions two children side by side. Divider persistence is
	// this component's concern; the math lives in ../splitpane for testability.
	//
	// Uses design tokens (--space-1) exclusively — no hardcoded spacing.
	// The right pane is a pixel width; the left pane takes the leftover.
	import {
		DEFAULT_RIGHT_PX,
		DIVIDER_HIT_MIN_PX,
		KEYBOARD_STEP_PX,
		MIN_RIGHT_PX,
		attachDragListeners,
		clampRightPx,
		maxRightPx,
		pointerToRightPx,
		readPersistedRightPx,
		writePersistedRightPx
	} from '../splitpane';

	interface Props {
		left: import('svelte').Snippet;
		right: import('svelte').Snippet;
	}

	let { left, right }: Props = $props();

	// Desired inspector width. Display is clamped to the live container so a
	// wide preference survives a shrink and comes back when the window grows.
	let desiredPx = $state(DEFAULT_RIGHT_PX);
	let containerWidth = $state(0);
	let rightPx = $derived(clampRightPx(desiredPx, containerWidth));
	// true while the user is actively dragging.
	let dragging = $state(false);

	let container: HTMLDivElement | undefined = $state();
	// Window listeners are the drag mechanism; pointer capture is only
	// an optimisation. WKWebView does not always honour capture, and
	// binding move/up to the 4px divider then latches `dragging` (TD-1011).
	let detachDrag: (() => void) | null = null;

	function persist() {
		writePersistedRightPx(window.localStorage, desiredPx);
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
		const next = pointerToRightPx(e.clientX, rect?.left ?? 0, rect?.width ?? 0);
		if (next !== null) desiredPx = next;
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
			desiredPx = clampRightPx(desiredPx + KEYBOARD_STEP_PX, containerWidth);
		} else if (e.key === 'ArrowRight') {
			desiredPx = clampRightPx(desiredPx - KEYBOARD_STEP_PX, containerWidth);
		} else {
			return;
		}
		e.preventDefault();
		persist();
	}

	// Restore the persisted divider position on mount. $effect runs
	// client-side only, so localStorage is safe to touch here.
	$effect(() => {
		desiredPx = readPersistedRightPx(window.localStorage);
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
	bind:clientWidth={containerWidth}
	class="splitpane"
	class:dragging
	data-dragging={dragging ? "true" : "false"}
	data-right-px={rightPx}
	style={`grid-template-columns: minmax(0, 1fr) var(--space-1) ${rightPx}px; --divider-hit: ${DIVIDER_HIT_MIN_PX}px;`}
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
		aria-label="Activity pane width"
		aria-valuenow={Math.round(rightPx)}
		aria-valuemin={MIN_RIGHT_PX}
		aria-valuemax={maxRightPx(containerWidth)}
		onpointerdown={onDividerDown}
		onkeydown={onDividerKeydown}
	></div>
	<div class="pane right">{@render right()}</div>
</div>

<style>
	.splitpane {
		display: grid;
		/* Fill the parent flex line; otherwise the grid shrink-wraps and
		   pixel columns resolve against an indefinite width. */
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
		background: var(--color-hairline);
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
