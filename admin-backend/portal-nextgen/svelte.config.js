import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { readFileSync } from 'node:fs';

// The app version players see in the footer, and the one
// ops/deploy-portal-nextgen.sh refuses to redeploy unchanged. package.json is
// the single source: SvelteKit stamps it into build/_app/version.json (live at
// /_app/version.json) and exports it as `version` from $app/environment. The
// default was Date.now(), which made every build look like a new release and
// left players nothing to quote back to us.
const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));

// V2 next-gen is a static SPA artifact rsynced to <web-host>, and the app is
// served at the ROOT of portal.lastsietch.com, so the base path is empty.
// lastsietch-admin still holds the files at /portal/v2 internally; a middleware
// rewrites a root-host request path onto them, which is why nothing in the
// build knows about that prefix. SPA fallback so client routing works without
// per-route prerender.
/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({ fallback: 'index.html', precompress: false, strict: false }),
    paths: { base: '', relative: false },
    appDir: '_app',
    version: { name: pkg.version },
  },
};

export default config;
