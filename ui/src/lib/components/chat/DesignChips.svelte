<script lang="ts">
	// Composer chips for Design-mode picks (TD-3403). Richer than a file
	// chip: xpath or role, attributes, box/styles, and the cropped frame.
	import { pickLabel, type DesignPick } from '../../design';
	import Icon from '../Icon.svelte';

	let {
		picks,
		onremove,
	}: {
		picks: readonly DesignPick[];
		onremove?: (id: string) => void;
	} = $props();

	function detail(pick: DesignPick): string {
		const attrs = Object.entries(pick.attributes)
			.map(([k, v]) => `${k}=${v}`)
			.join(' ');
		const box = `${Math.round(pick.box.width)}×${Math.round(pick.box.height)} at ${Math.round(pick.box.x)},${Math.round(pick.box.y)}`;
		const styles = Object.entries(pick.styles)
			.map(([k, v]) => `${k}: ${v}`)
			.join('; ');
		return [pick.xpath, pick.role, attrs, box, styles].filter((s) => s !== null && s !== '').join(' · ');
	}
</script>

{#if picks.length > 0}
	<ul class="chips" aria-label="Design picks">
		{#each picks as pick (pick.id)}
			<li class="chip">
				{#if pick.cropDataUrl !== ''}
					<img class="thumb" src={pick.cropDataUrl} alt="" />
				{:else}
					<Icon name="box" size={12} />
				{/if}
				<span class="name" title={detail(pick)}>{pickLabel(pick)}</span>
				<span class="meta">{Math.round(pick.box.width)}×{Math.round(pick.box.height)}</span>
				{#if onremove}
					<button
						type="button"
						class="remove"
						title="Remove design pick"
						aria-label="Remove design pick {pickLabel(pick)}"
						onclick={() => onremove(pick.id)}
					>
						<Icon name="x" size={12} />
					</button>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-2);
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1);
		max-width: 100%;
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-xs);
		color: var(--color-ink-secondary);
		background: var(--color-sunken);
		border: var(--border-width) solid var(--color-hairline);
		border-radius: var(--radius-full);
	}

	.thumb {
		width: var(--space-4);
		height: var(--space-4);
		object-fit: cover;
		border-radius: var(--radius-sm);
		flex-shrink: 0;
	}

	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--color-ink);
		font-family: var(--font-mono);
	}

	.meta {
		flex-shrink: 0;
		font-family: var(--font-mono);
		color: var(--color-ink-muted);
	}

	.remove {
		display: inline-flex;
		align-items: center;
		flex-shrink: 0;
		padding: 0;
		color: var(--color-ink-muted);
		background: transparent;
		border: none;
		cursor: pointer;
	}

	.remove:hover {
		color: var(--color-ink);
	}

	.remove:focus-visible {
		outline: 2px solid var(--color-accent);
		outline-offset: 1px;
		border-radius: var(--radius-sm);
	}
</style>
