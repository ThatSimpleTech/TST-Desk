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
	import { chatJump } from "../../chat-jump.svelte.js";

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
	// The row a jump landed on, ringed for a moment so the eye finds it.
	let targetId: string | null = $state(null);
	// The last jump this list answered — a fresh mount must not replay one.
	let answeredJump = 0;

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
	//
	// The dependencies are read explicitly and the store call is untracked,
	// which is load-bearing: `$virtualizer` is a store, so reading it here
	// would subscribe this effect to it, and `setOptions` notifies that
	// store's subscribers. The effect would then re-trigger itself forever —
	// Svelte reports it as `effect_update_depth_exceeded`, and because the
	// error is thrown out of the runtime it stops processing *any* further
	// updates, so the whole window goes unresponsive rather than just the
	// transcript (TD-1012).
	$effect(() => {
		const count = messages.length;
		void scrollEl;
		untrack(() => {
			$virtualizer.setOptions({
				count,
				getScrollElement: () => scrollEl,
				estimateSize: () => 96,
				getItemKey: (index: number) => messages[index]?.id ?? index,
				overscan: 6,
			});
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

	// Jump-to-turn from the Activity pane (navigation round, 2026-09). The
	// nonce is the trigger; the list finds the user message that opened the
	// turn and scrolls it to the top. Scrolling up unpins auto-scroll the way
	// any scroll up does, so the jump button appears and the stream does not
	// drag the reader back down. Messages and the virtualizer are read
	// untracked so growth mid-stream cannot replay the jump.
	$effect(() => {
		const nonce = chatJump.nonce;
		const turnId = chatJump.turnId;
		if (nonce === 0 || nonce === answeredJump || turnId === null) return;
		answeredJump = nonce;
		const index = untrack(() =>
			messages.findIndex((m) => m.role === "user" && m.turnId === turnId),
		);
		if (index < 0) return;
		const id = untrack(() => messages[index]?.id ?? null);
		untrack(() => $virtualizer.scrollToIndex(index, { align: "start" }));
		targetId = id;
		const timer = setTimeout(() => {
			targetId = null;
		}, 1800);
		return () => clearTimeout(timer);
	});
</script>

<div class="list-wrap">
	<div class="scroll" bind:this={scrollEl}>
		<div class="sizer" style="height: {$virtualizer.getTotalSize()}px;">
			{#each $virtualizer.getVirtualItems() as row (row.key)}
				<div
					class="row"
					class:row--target={row.key === targetId}
					data-index={row.index}
					use:measure
					style="transform: translateY({row.start}px);"
				>
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

	/* A jump's landing row: ringed in accent for a moment. Drawn as an
	   overlay inside the row's own box (minus the gap below it), so the
	   list's edges never clip it. */
	.row--target::before {
		content: "";
		position: absolute;
		inset: 0 0 var(--space-3) 0;
		border: 2px solid var(--color-accent);
		border-radius: var(--radius-lg);
		pointer-events: none;
		animation: settle var(--dur-enter) var(--ease-out);
	}

	@keyframes settle {
		from {
			opacity: 0;
			transform: scale(1.02);
		}
		to {
			opacity: 1;
			transform: none;
		}
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
		color: var(--color-on-accent);
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
