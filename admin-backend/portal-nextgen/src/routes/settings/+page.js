// Every panel fetches client-side in its own component (once auth resolves) so
// the carved shell renders instantly instead of blocking on the identity code
// and the activity log. Matches the storage/character routes; ssr=false is
// inherited from the root +layout.js and the static adapter prerenders only the
// shell.
