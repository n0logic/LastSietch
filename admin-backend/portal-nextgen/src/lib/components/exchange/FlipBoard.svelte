<script>
  // The Bot-Floor flip board: templates whose cheapest player ask sits UNDER what
  // the CHOAM bot will pay for it, so buying the ask and selling to the bot nets the
  // spread. Server-computed (exchange.flips). Each row opens the price ladder. The
  // spread bar is AMBER: it is a verifiable derived value from one snapshot, not a
  // live-moving number, so no Ibad. Rows carry the buy/cap numerals too (never bar-only).
  import { exchange, openItem } from '$lib/exchange.svelte.js';
  import { iconUrl } from '$lib/icons.js';
  import SpreadBar from './SpreadBar.svelte';

  let rows = $derived(exchange.flips.rows || []);
  let status = $derived(exchange.flips.status);
</script>

<div class="flipboard">
  <div class="head">
    <p class="kicker mono">Bot-Floor flips</p>
    <p class="sub">Player asks below the CHOAM bot's buy price.</p>
  </div>

  {#if status === 'loading'}
    <p class="hint mono">scanning the floor&hellip;</p>
  {:else if status === 'error'}
    <p class="hint">Could not read the flip board.</p>
  {:else if rows.length === 0}
    <p class="hint">No flips on the board right now. The bot floor is holding.</p>
  {:else}
    <ul class="rows">
      {#each rows as r (r.template_id)}
        <li>
          <button class="flip" type="button" onclick={() => openItem(r)} aria-label="{r.name}, open price ladder">
            <img class="icon" src={iconUrl(r.icon)} alt="" aria-hidden="true" loading="lazy" />
            <span class="name" title={r.name}>{r.name || r.template_id}{#if r.grade != null}<span class="grade mono">G{r.grade}</span>{/if}</span>
            <span class="ask mono">{Number(r.min_ask).toLocaleString()}<span class="arrow">&rarr;</span>{Number(r.bot_cap).toLocaleString()}</span>
            <span class="bar"><SpreadBar minAsk={r.min_ask} botCap={r.bot_cap} /></span>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .flipboard { display: flex; flex-direction: column; gap: var(--space-3); }
  .head { display: flex; flex-direction: column; gap: 2px; }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .2em; font-size: var(--text-xs); }
  .sub { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .hint { margin: var(--space-1) 0; font-size: var(--text-sm); color: var(--text-muted); }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); max-height: 20rem; overflow-y: auto; }
  .flip {
    width: 100%; display: grid; align-items: center; gap: var(--space-2);
    grid-template-columns: 28px minmax(0, 1fr) auto; grid-template-rows: auto auto;
    padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer; text-align: left;
    transition: border-color var(--motion-fast) var(--ease-out);
  }
  .flip:hover { border-color: var(--accent); }
  .icon { grid-row: 1 / 3; width: 28px; height: 28px; object-fit: contain; }
  .name { min-width: 0; font-size: var(--text-sm); color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: inline-flex; align-items: baseline; gap: var(--space-2); }
  .grade { font-size: 10px; color: var(--text-muted); }
  .ask { font-size: var(--text-xs); color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; justify-self: end; }
  .ask .arrow { margin: 0 4px; color: var(--accent-soft); }
  .bar { grid-column: 2 / 4; }
</style>
