// Data is fetched client-side in +page.svelte (once auth resolves) so the carved
// shell renders instantly instead of blocking on the Karum overview request.
// Matches the bases/storage/exchange/rewards routes; the static adapter prerenders
// only the shell. Browsing the board is public, so the signed-out path still loads
// the board and seals only the stall.
