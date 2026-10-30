<script>
  // "Needs you": up to three real things waiting (see needsYou() in shellHome.js).
  // Props only. Empty is a statement, not a blank: nothing is waiting.
  import { base } from '$app/paths';
  let { items = [], signedIn = false } = $props();
</script>

<section class="card" aria-labelledby="ny-h">
  <div class="head"><h2 id="ny-h">Needs you</h2><a href={`${base}/mailbox`}>Inbox <span aria-hidden="true">↗</span></a></div>
  {#if !signedIn}
    <p class="quiet">Sign in to see what is waiting for you.</p>
  {:else if items.length === 0}
    <p class="quiet">Nothing is waiting. You are caught up.</p>
    <a class="more" href={`${base}/activity`}>Since your last visit <span aria-hidden="true">↗</span></a>
  {:else}
    <ul>
      {#each items as it}
        <li>
          <a href={it.href}>
            <span class="t"><b>{it.title}</b><span>{it.note}</span></span>
            <span class="cta">{it.cta}</span>
          </a>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .card { padding: 20px; background: var(--panel); border: 1px solid var(--edge); border-radius: 6px; min-width: 0; }
  .head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; gap: 12px; }
  h2 { margin: 0; font: 700 24px var(--font-display); letter-spacing: .06em; text-transform: uppercase; }
  .head a, .more { font-size: var(--text-sm); text-decoration: none; }
  ul { list-style: none; margin: 0; padding: 0; }
  li a { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 60px; padding: 10px 0; border-top: 1px solid var(--border-subtle); text-decoration: none; }
  li:first-child a { border-top: 0; }
  .t { display: grid; gap: 2px; min-width: 0; }
  .t b { font: 600 17px var(--font-display); letter-spacing: .04em; }
  .t span { font-size: var(--text-sm); color: var(--text-muted); }
  .cta { flex: none; display: inline-flex; align-items: center; min-height: 36px; padding: 0 14px; border-radius: 4px; border: 1px solid var(--edge-hi); font: 600 13px var(--font-display); letter-spacing: .12em; text-transform: uppercase; color: var(--text); }
  .cta:hover { border-color: var(--accent-soft); color: var(--accent-text); }
  .quiet { margin: 0 0 8px; color: var(--text-muted); }
</style>
