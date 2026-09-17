// Data is fetched client-side in +page.svelte (once auth resolves) so the carved
// shell renders instantly instead of blocking on the market overview request.
// Matches the storage/maps/guilds routes; ssr=false is inherited from the root
// +layout.js, and the static adapter prerenders only the shell.
