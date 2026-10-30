// The contract between a background's drawing component and the shared
// canvas host, AmbientCanvas.svelte (TD-5202).

export interface Scene {
	/** Rebuild for a new size in CSS px. Allocate here, never in draw. */
	resize(width: number, height: number): void;
	/** Re-read colours from the design tokens: on mount and on every theme switch. */
	recolor(read: (token: string) => string, doc: Document): void;
	/** Paint the frame at `clockMs`; the context is already in CSS px. */
	draw(ctx: CanvasRenderingContext2D, clockMs: number): void;
}

/** Every background component takes the same two props, so the host and the
 *  Settings preview tiles can render any of them. */
export interface BackgroundProps {
	/** Run frames (true) or hold a still frame. */
	animate: boolean;
	/** "window": fixed behind the whole app. "parent": fill the nearest
	 *  positioned box (a preview tile, the chat pane). */
	fill?: "window" | "parent";
}
