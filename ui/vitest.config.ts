import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	// Svelte plugin so *.svelte.ts rune modules (e.g. session-status.svelte.ts,
	// TD-1006) resolve and compile in the test pipeline; without it vitest
	// can't load $state-bearing modules.
	plugins: [svelte()],
	test: {
		include: ['src/**/*.test.ts'],
		environment: 'node'
	}
});