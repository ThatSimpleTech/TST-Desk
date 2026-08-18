<script lang="ts">
	// Test-only mount surface for TD-1011. Snippets cannot be built from a
	// .ts file, so the regression test mounts this instead of SplitPane
	// directly.
	import SplitPane from "./SplitPane.svelte";
</script>

<div class="frame">
	<SplitPane>
		{#snippet left()}<div class="side">L</div>{/snippet}
		{#snippet right()}<div class="side">R</div>{/snippet}
	</SplitPane>
</div>

<style>
	.frame {
		width: 1000px;
		height: 200px;
	}

	.side {
		height: 100%;
	}
</style>
