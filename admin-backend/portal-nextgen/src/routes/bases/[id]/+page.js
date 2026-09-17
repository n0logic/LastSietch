// Data is fetched client-side in +page.svelte from the route param, so the carved
// shell renders instantly instead of blocking on the listing request. Matches the
// bases/storage routes; the static adapter emits one fallback index.html, which is
// what serves this dynamic segment, so there is nothing here to prerender.
