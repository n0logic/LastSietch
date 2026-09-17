// Pure client SPA: data loads at runtime from the FastAPI JSON API, so no SSR and
// no prerender (the static adapter emits a fallback index.html for all routes).
export const ssr = false;
export const prerender = false;
