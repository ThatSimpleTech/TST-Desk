// Which component paints each background behind the window (TD-5202). The
// host renders one of these full-window; the Settings tiles render the same
// component inside a tile, so a preview is the real thing, not a picture.

import type { Component } from "svelte";
import type { BackgroundId } from "../../backgrounds/catalog";
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
