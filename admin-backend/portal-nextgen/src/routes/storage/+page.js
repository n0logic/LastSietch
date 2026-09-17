// Data is fetched client-side in +page.svelte (once auth resolves) so the carved
// shell renders instantly instead of blocking on the storage overview request.
// Matches the maps/guilds routes; the static adapter prerenders only the shell.
