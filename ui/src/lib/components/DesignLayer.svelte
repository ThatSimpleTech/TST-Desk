<script lang="ts">
	// Overlay on the frozen Screen frame (TD-3403). One pointer path:
	// click replaces, shift-click adds, shift-drag is a region pick.
	// No glow and no agent cursor — those are TD-3402.
	import {
		clampBox,
		cropFromImage,
		frameToOverlay,
		mapClientToFrame,
		movedEnough,
		rectFromPoints,
		type CssPoint,
	} from '../design';
	import { addPick, design } from '../design.svelte.js';

	let {
		image = null,
	}: {
		/** The frozen frame. Null while the pane is empty. */
		image?: HTMLImageElement | null;
	} = $props();

	let layerEl: HTMLDivElement | null = $state(null);
	let dragStart = $state<CssPoint | null>(null);
	let dragCurrent = $state<CssPoint | null>(null);
	let shiftDrag = $state(false);

	let selecting = $derived(design.enabled && !design.actuating && image !== null);
	let overlaySize = $derived({
		width: layerEl?.clientWidth ?? image?.width ?? 0,
		height: layerEl?.clientHeight ?? image?.height ?? 0,
	});

	function frameSize(): { width: number; height: number } {
		if (image === null) return { width: 0, height: 0 };
		return {
			width: image.naturalWidth || image.width,
			height: image.naturalHeight || image.height,
		};
	}

	function pointFromEvent(event: PointerEvent): CssPoint | null {
		if (image === null || layerEl === null) return null;
		const rect = layerEl.getBoundingClientRect();
		const natural = frameSize();
		return mapClientToFrame(event.clientX, event.clientY, rect, natural.width, natural.height);
	}

	function onPointerDown(event: PointerEvent): void {
		if (!selecting) return;
		const point = pointFromEvent(event);
		if (point === null) return;
		event.preventDefault();
		layerEl?.setPointerCapture(event.pointerId);
		dragStart = point;
		dragCurrent = point;
		shiftDrag = event.shiftKey;
	}

	function onPointerMove(event: PointerEvent): void {
		if (dragStart === null) return;
		const point = pointFromEvent(event);
		if (point === null) return;
		dragCurrent = point;
	}

	async function onPointerUp(event: PointerEvent): Promise<void> {
		if (dragStart === null || image === null) return;
		const end = pointFromEvent(event) ?? dragCurrent ?? dragStart;
		const start = dragStart;
		const wasShift = shiftDrag || event.shiftKey;
		const region = wasShift && movedEnough(start, end);
		dragStart = null;
		dragCurrent = null;
		shiftDrag = false;
		const natural = frameSize();
		const box = clampBox(
			region ? rectFromPoints(start, end) : { x: end.x, y: end.y, width: 1, height: 1 },
			natural,
		);
		const crop = cropFromImage(image, region ? box : clampBox({ x: end.x - 20, y: end.y - 10, width: 80, height: 24 }, natural));
		await addPick({
			x: region ? box.x + box.width / 2 : end.x,
			y: region ? box.y + box.height / 2 : end.y,
			box: region ? box : undefined,
			cropDataUrl: crop,
			mode: wasShift ? 'add' : 'replace',
		});
	}
</script>

<div
	bind:this={layerEl}
	class="layer"
	class:active={selecting}
	role={selecting ? 'group' : undefined}
	aria-label={selecting ? 'Design mode. Click to select an element.' : undefined}
	onpointerdown={onPointerDown}
	onpointermove={onPointerMove}
	onpointerup={onPointerUp}
>
	{#if image !== null}
		{#each design.picks as pick (pick.id)}
			{@const overlay = frameToOverlay(pick.box, overlaySize, image.naturalWidth, image.naturalHeight)}
			<div
				class="box"
				style:left={`${overlay.x}px`}
				style:top={`${overlay.y}px`}
				style:width={`${overlay.width}px`}
				style:height={`${overlay.height}px`}
			></div>
		{/each}
		{#if dragStart !== null && dragCurrent !== null && shiftDrag && movedEnough(dragStart, dragCurrent)}
			{@const live = rectFromPoints(dragStart, dragCurrent)}
			{@const overlay = frameToOverlay(live, overlaySize, image.naturalWidth, image.naturalHeight)}
			<div
				class="box live"
				style:left={`${overlay.x}px`}
				style:top={`${overlay.y}px`}
				style:width={`${overlay.width}px`}
				style:height={`${overlay.height}px`}
			></div>
		{/if}
	{/if}
</div>

<style>
	.layer {
		position: absolute;
		inset: 0;
		pointer-events: none;
	}

	.layer.active {
		pointer-events: auto;
	}

	.box {
		position: absolute;
		border: var(--border-width) solid var(--color-accent);
		background: color-mix(in srgb, var(--color-accent) 12%, transparent);
		pointer-events: none;
	}

	.box.live {
		background: color-mix(in srgb, var(--color-accent) 8%, transparent);
	}
</style>
