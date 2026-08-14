<script lang="ts">
	// The one icon renderer (TD-1608). Glyphs come from the shared map in
	// ../icons.ts — never paste SVG at a call site. Stroke inherits
	// currentColor, so an icon always matches the text it sits beside;
	// default size is sized-to-text (1em), override with `size` in px.
	// `filled` is reserved for active states.
	import { ICONS, type IconName } from '../icons';

	let {
		name,
		size,
		filled = false,
	}: {
		name: IconName;
		/** Rendered edge in px; defaults to 1em (sized to surrounding text). */
		size?: number;
		filled?: boolean;
	} = $props();
</script>

<svg
	class="icon"
	viewBox="0 0 24 24"
	width={size ?? '1em'}
	height={size ?? '1em'}
	fill={filled ? 'currentColor' : 'none'}
	stroke="currentColor"
	stroke-width="1.5"
	stroke-linecap="round"
	stroke-linejoin="round"
	aria-hidden="true"
>
	<!-- Glyphs are static constants from ../icons.ts — never user input. -->
	{@html ICONS[name]}
</svg>

<style>
	.icon {
		display: inline-block;
		vertical-align: -0.125em;
		flex-shrink: 0;
	}
</style>
