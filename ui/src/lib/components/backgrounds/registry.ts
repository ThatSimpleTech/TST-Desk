// Which component paints each background behind the window (TD-5202). The
// host renders one of these full-window; the Settings tiles render the same
// component inside a tile, so a preview is the real thing, not a picture.

import type { Component } from "svelte";
import type { BackgroundId } from "../../backgrounds/catalog";
import type { BackgroundStrength } from "../../backgrounds/prefs";
import type { BackgroundProps } from "../../backgrounds/scene";
import BackgroundOrbit from "../../BackgroundOrbit.svelte";
import AuroraBackground from "./AuroraBackground.svelte";
import ConstellationsBackground from "./ConstellationsBackground.svelte";
import DeepSpaceBackground from "./DeepSpaceBackground.svelte";
import FirefliesBackground from "./FirefliesBackground.svelte";
import GalaxyBackground from "./GalaxyBackground.svelte";

export const WINDOW_LAYERS: Record<BackgroundId, Component<BackgroundProps> | null> = {
	off: null,
	classic: BackgroundOrbit,
	galaxy: GalaxyBackground,
	"deep-space": DeepSpaceBackground,
	aurora: AuroraBackground,
	constellations: ConstellationsBackground,
	fireflies: FirefliesBackground,
};

/** The token for the ground-coloured veil the host lays over each
 *  background behind the panes. Text sits anywhere in the chat column and
 *  the Activity pane, and both panes are clear, so the veil is one even
 *  sheet over the whole window: no step where the panes meet. Each
 *  background's is as strong as its brightest light needs; Classic keeps
 *  its own look and Off has nothing to quiet. */
export const WINDOW_VEILS: Record<BackgroundId, string | null> = {
	off: null,
	classic: null,
	galaxy: "--bg-galaxy-veil",
	"deep-space": "--bg-space-veil",
	aurora: "--bg-aurora-veil",
	constellations: "--bg-constellation-veil",
	fireflies: "--bg-firefly-veil",
};

/** The veil's fill at a strength. The veil token stays the one source of
 *  its colour. Vivid mixes it with clear, which scales only its alpha, at
 *  the share `--bg-veil-vivid` gives, or at the background's own
 *  `--bg-*-veil-vivid` where its brightest light needs more for body text
 *  to keep 4.5:1 (tokens.css; the numbers are in DECISIONS.md). A fill,
 *  not an opacity on the box: an opacity under 1 on a full-window box
 *  costs a full-window transparency layer on every repaint where WebKitGTK
 *  paints in software. */
export function veilFill(token: string, strength: BackgroundStrength): string {
	if (strength === "subtle") return `var(${token})`;
	return `color-mix(in srgb, var(${token}) var(${token}-vivid, var(--bg-veil-vivid)), transparent)`;
}
