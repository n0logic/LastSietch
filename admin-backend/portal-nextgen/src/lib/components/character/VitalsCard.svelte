<script>
  // Vitals: the character's raw currency + progression scalars, each a stat slab
  // with its glyph. The JSON returns RAW ints (contract MODULE 1); we format
  // client-side. A missing stat glyph resolves to null and the slab omits it.
  import { statIconUrl } from '$lib/icons.js';

  let { vitals = null, lastLogout = false } = $props();

  // slug -> stat mount glyph + label. Order = render order.
  const STATS = [
    { key: 'bank_solari', label: 'Bank Solari', icon: 'solari-bank' },
    { key: 'pocket_solari', label: 'Pocket Solari', icon: 'solari' },
    { key: 'scrip', label: 'Scrip', icon: 'scrip' },
    { key: 'intel', label: 'Intel', icon: 'intel' },
    { key: 'xp', label: 'Total XP', icon: 'spec-combat' },
    { key: 'unspent_sp', label: 'Unspent SP', icon: 'spec-crafting' },
  ];

  function fmt(n) {
    const v = Number(n);
    return Number.isFinite(v) ? v.toLocaleString() : '0';
  }
  let rows = $derived(STATS.map((s) => ({ ...s, value: vitals ? vitals[s.key] : null })));
</script>

<div class="vitals">
  <p class="panel-kicker mono">Vitals</p>
  {#if !vitals}
    <p class="hollow">Vitals could not be read right now.</p>
  {:else}
    <div class="grid">
      {#each rows as s (s.key)}
        {@const url = statIconUrl(s.icon)}
        <div class="slab">
          {#if url}<img class="glyph" src={url} alt="" aria-hidden="true" loading="lazy" />{/if}
          <div class="body">
            <span class="num mono">{fmt(s.value)}</span>
            <span class="lbl">{s.label}</span>
          </div>
        </div>
      {/each}
    </div>
    {#if lastLogout}
      <p class="note mono">Balances reflect your last logout.</p>
    {/if}
  {/if}
</div>

<style>
  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: var(--space-2); }
  .slab {
    display: flex; align-items: center; gap: var(--space-2); min-width: 0;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3);
  }
  .glyph { width: 28px; height: 28px; object-fit: contain; flex: 0 0 auto; }
  .body { display: flex; flex-direction: column; min-width: 0; }
  .num { font-size: 17px; font-weight: 700; color: var(--text); line-height: 1.1; white-space: nowrap; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .note { margin: var(--space-3) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
</style>
