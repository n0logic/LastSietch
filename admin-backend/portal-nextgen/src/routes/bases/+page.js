// Data is fetched client-side in +page.svelte (once auth resolves) so the carved
// shell renders instantly instead of blocking on the bases overview request.
// Matches the storage/exchange/rewards routes; the static adapter prerenders only
// the shell.
