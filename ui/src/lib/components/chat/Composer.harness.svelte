<script lang="ts">
	// Test-only mount surface for TD-4501: the slash menu lives in
	// Composer's markup and keydown handlers, so its behaviour is only
	// reachable through a real mount. The harness owns the draft binding
	// and records submits; tests drive it through the exported functions.
	import Composer from "./Composer.svelte";
	import type { AttachmentDraft } from "../../attachments";
	import type { AttachmentLimits } from "../../protocol";

	let value = $state("");
	let submitted: { text: string; attachments: readonly AttachmentDraft[] }[] = $state([]);

	const limits: AttachmentLimits = {
		max_file_bytes: 100_000,
		max_total_bytes: 300_000,
		max_count: 5,
	};

	export function type(text: string): void {
		value = text;
	}

	export function draft(): string {
		return value;
	}

	export function submits(): { text: string; attachments: readonly AttachmentDraft[] }[] {
		return submitted;
	}
</script>

<Composer
	bind:value
	{limits}
	workspacePath="/w"
	onsubmit={(text, attachments) => {
		submitted = [...submitted, { text, attachments }];
	}}
/>
