// Data is fetched client-side in +page.svelte (the store subscribes on mount,
// once auth resolves) so the carved shell renders instantly instead of blocking
// on the channel read. Matches the mailbox/storage/events routes; ssr=false is
// inherited from the root +layout.js and the static adapter prerenders only the
// shell.
//
// Chat is linked-accounts only for reading AND posting, so there is nothing
// public to prerender here: the signed-out path renders the sealed panel and
// makes no request at all.
