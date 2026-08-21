import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import type { KitConfig } from '@sveltejs/kit';
import { defineConfig } from 'vite';

// Content security policy for the webview (TD-4807). Emitted as a
// build-time <meta> tag by kit.csp: hash mode pins the SvelteKit inline
// bootstrap per build, which a static policy in tauri.conf.json cannot
// do — the bootstrap imports content-hashed chunks, so its own hash
// changes every build. Tauri amends the meta at serve time with nonces
// for its bridge scripts (dangerousDisableAssetCspModification stays
// false for that reason). Exported for src/csp-config.test.ts.
export function contentSecurityPolicy(mode: string): NonNullable<KitConfig['csp']> {
	return {
		mode: 'hash',
		directives: {
			'default-src': ['self'],
			'script-src': ['self'],
			// Component chrome positions itself with inline style
			// attributes (cursor, glow); SvelteKit hashes anything it
			// emits inline on top of this.
			'style-src': ['self', 'unsafe-inline'],
			'connect-src': [
				'self',
				// The daemon socket; the port is per-run, hence the wildcard.
				'ws://127.0.0.1:*',
				// Tauri IPC fallback channel (Windows/Linux).
				'ipc://localhost',
				// Vite HMR, dev only.
				...(mode === 'development' ? (['ws://localhost:*'] as const) : [])
			],
			// Screen-frame previews arrive as data: URLs via the host reader.
			'img-src': ['self', 'data:'],
			'font-src': ['self'],
			'object-src': ['none'],
			'base-uri': ['none'],
			'form-action': ['none']
		}
	};
}

export default defineConfig(({ mode }) => ({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// Tauri serves static files from ui/build. Emit a prerendered
			// index.html plus a 200.html fallback for client-side routes.
			adapter: adapter({ fallback: '200.html' }),

			csp: contentSecurityPolicy(mode)
		})
	]
}));
