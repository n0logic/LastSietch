// Data is fetched client-side in +page.svelte (the store subscribes on mount) so
// the carved shell renders instantly instead of blocking on the events request.
// Matches the bases/karum/storage routes; ssr=false is inherited from the root
// +layout.js and the static adapter prerenders only the shell. Browsing the
// board is public, so the signed-out path loads exactly the same two lists.
