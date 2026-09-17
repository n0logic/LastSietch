// The queue loads client-side in +page.svelte, once the auth gate has resolved
// and the channels payload has said whether this viewer is an admin or a guild
// leader. Nothing is fetched here: a load() would run before either answer is
// known and would ask the server questions it is going to refuse. Matches the
// events/storage routes; ssr=false is inherited from the root +layout.js and the
// static adapter prerenders only the shell.
