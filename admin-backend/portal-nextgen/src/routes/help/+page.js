// Comment-only, the settings convention. The page renders from a static content
// module, so there is nothing to load before it paints; the one read it does make
// (the public feature flags) happens on mount in the component, because a page
// that blocked on it would show nothing at all when that endpoint is down.
// ssr=false is inherited from the root +layout.js and the static adapter
// prerenders only the shell.
