<script>
  // One carved member chip in the presence ribbon. LiveDot wears Ibad only when
  // the member is genuinely Online; offline members get an idle (muted) dot and
  // an honest last-seen caption. Self is marked with text ("you"), not colour.
  import LiveDot from '$lib/components/LiveDot.svelte';

  let { member } = $props();

  let online = $derived(member?.online_status === 'Online');
  let self = $derived(member?.is_self === true);
  let name = $derived(member?.character_name || 'Unknown survivor');
  // Presence is binary: online_status === 'Online' (case-sensitive) or OFFLINE.
  // Anything else -- a blank, a status we do not recognise, a row the census could
  // not resolve -- is offline, never "unknown". The last-seen stamp is appended
  // verbatim from the feed when there is one, and simply omitted when there is not.
  let caption = $derived(
    online ? 'online now'
           : (member?.last_activity ? `offline · last seen ${member.last_activity}` : 'offline')
  );
</script>

<span class="token" class:online class:self title={caption}>
  <LiveDot tone={online ? 'live' : 'idle'} />
  <span class="name">{name}</span>
  {#if self}<span class="you mono">you</span>{/if}
  <span class="cap mono">{caption}</span>
</span>

<style>
  .token {
    display: inline-flex; align-items: center; gap: var(--space-2);
    padding: var(--space-1) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
    max-width: 100%;
  }
  .token.self { border-color: color-mix(in srgb, var(--accent) 45%, var(--edge)); }
  .name {
    font-size: var(--text-sm); color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .token.online .name { color: var(--text); }
  .token:not(.online) .name { color: var(--text-muted); }
  .you {
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent-text);
  }
  .cap {
    font-size: var(--text-xs); color: var(--text-muted);
    letter-spacing: .04em; white-space: nowrap;
  }
  /* On a tight ribbon the last-seen caption is the first thing to drop. */
  @media (max-width: 560px) {
    .cap { display: none; }
  }
</style>
