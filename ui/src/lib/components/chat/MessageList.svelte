<script lang="ts">
	// Virtualized conversation history (TD-1004).
	//
	// @tanstack/svelte-virtual windows the rows — a thousand-message session
	// renders only what's near the viewport, with dynamic measurement for
	// variable-height markdown. Auto-scroll follows new content while pinned
	// and stops the moment the user scrolls up; the jump button re-pins.
	import { untrack } from "svelte";
	import { createVirtualizer } from "@tanstack/svelte-virtual";
	import { createAutoScroll, type AutoScroll } from "../../autoscroll";
	import type { ChatMessage } from "../../chat-store";
	import MessageBubble from "./MessageBubble.svelte";
	import Icon from "../Icon.svelte";

	let {
		messages,
		turnLive = false,
		onretry,
	}: {
		messages: ChatMessage[];
		/** A turn is in flight — message actions use it to stand retry down (TD-1606). */
		turnLive?: boolean;
		onretry?: () => void;
	} = $props();

	let scrollEl: HTMLDivElement | null = $state(null);
	let auto: AutoScroll | null = $state(null);

	// Initial count is 0 on purpose — the $effect below is the source of
	// truth and re-sets options reactively whenever the conversation grows.
	const virtualizer = createVirtualizer<HTMLDivElement, HTMLDivElement>({
		count: 0,
		getScrollElement: () => scrollEl,
		estimateSize: () => 96,
		getItemKey: (index: number) => messages[index]?.id ?? index,
		overscan: 6,
	});

	// Keep the virtualizer in sync as the conversation grows.
	$effect(() => {
		$virtualizer.setOptions({
			count: messages.length,
			getScrollElement: () => scrollEl,
			estimateSize: () => 96,
			getItemKey: (index: number) => messages[index]?.id ?? index,
			overscan: 6,
		});
	});

	$effect(() => {
		if (scrollEl === null) return;
		const instance = createAutoScroll(scrollEl);
		auto = instance;
		return () => {
			instance.destroy();
			auto = null;
		};
	});

	// Follow new content while pinned. Track both the count and the streaming
	// tail's text so growth mid-stream follows too.
	$effect(() => {
		const last = messages[messages.length - 1];
		void messages.length;
		void last?.text;
		untrack(() => auto?.maybeFollow());
	});

	// Dynamic row measurement: virtual-core reads data-index off the node.
	function measure(node: HTMLDivElement) {
		$virtualizer.measureElement(node);
		return {
			destroy() {
				$virtualizer.measureElement(null);
			},
		};
	}
</script>

<div class="list-wrap">
	<div class="scroll" bind:this={scrollEl}>
		<div class="sizer" style="height: {$virtualizer.getTotalSize()}px;">
			{#each $virtualizer.getVirtualItems() as row (row.key)}
				<div class="row" data-index={row.index} use:measure style="transform: translateY({row.start}px);">
					<MessageBubble message={messages[row.index]} first={row.index === 0} {turnLive} {onretry} />
				</div>
			{/each}
		</div>
	</div>
	{#if auto !== null && !auto.pinned}
		<button class="jump" type="button" onclick={() => auto?.jumpToLatest()}>
			<Icon name="chevron-down" size={14} /> Jump to latest
		</button>
	{/if}
</div>

<style>
	.list-wrap {
		position: relative;
		flex: 1;
		min-height: 0;
	}

	.scroll {
		height: 100%;
		overflow-y: auto;
		/* Reserving the gutter keeps the scrollbar's arrival from reflowing
		   the conversation — part of the no-layout-jump criterion. */
		scrollbar-gutter: stable;
		padding: var(--space-4);
	}

	.sizer {
		position: relative;
		width: 100%;
	}

	.row {
		position: absolute;
		top: 0;
		left: 0;
		width: 100%;
		padding-bottom: var(--space-3);
	}

	.jump {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		position: absolute;
		bottom: var(--space-4);
		left: 50%;
		transform: translateX(-50%);
		padding: var(--space-2) var(--space-4);
		font-size: var(--text-sm);
		font-weight: var(--weight-medium);
		color: var(--color-accent-text);
		background: var(--color-accent);
		border: none;
		border-radius: var(--radius-full);
		box-shadow: var(--shadow-md);
		cursor: pointer;
		transition: background var(--transition-fast);
	}

	.jump:hover {
		background: var(--color-accent-hover);
	}
</style>
