import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { reviewPreview } from './scripts/review-preview.mjs';

// Dev proxy: the app routes live at the root (served by SvelteKit dev), while
// the JSON API lives at /portal/<api> on the live host. Proxy ONLY the API
// prefixes so `npm run dev` reads real data + cookies.
// PWA (vite-plugin-pwa) is added in the next N0 slice once the base build is green.
const API_PREFIXES = [
  '/portal/maps',
  '/portal/me',
  '/portal/announcement',
  '/portal/login',
  '/portal/logout',
  '/portal/oauth',
  '/portal/storage',
  '/portal/containers',
  '/portal/exchange',
  '/portal/landsraad',
  '/portal/guilds',
  '/portal/server',
  '/portal/solido',
  // Dev-only ASSET proxying (not API): map card art + legend icons live under
  // /admin/static, the Hagga backdrop under /assets. Without these the dev
  // server 404s images that are fine in prod (same origin there).
  '/admin/static',
  '/assets',
];

const proxy = {};
for (const p of API_PREFIXES) {
  proxy[p] = { target: 'https://lastsietch.com', changeOrigin: true, secure: true };
}

export default defineConfig({
  define: { 'import.meta.env.VITE_REVIEW_PREVIEW': JSON.stringify(process.env.LASTSIETCH_PREVIEW === '1' ? 'true' : 'false') },
  plugins: [sveltekit(), ...(process.env.LASTSIETCH_PREVIEW === '1' ? [reviewPreview()] : [])],
  server: { proxy: process.env.LASTSIETCH_PREVIEW === '1' ? {} : proxy },
});
