<script>
  // One community-market blueprint card. Thumbnail (or a faction-tinted empty
  // art), title, author, the census the payload already carries (piece /
  // structural / placeable / pentashield counts, downloads, faction, age), tags,
  // and an MTX badge for paid pieces. "Preview 3D" opens the box viewer; "Save"
  // (linked viewers only) opens the import dialog prefilled with this listing.
  //
  // The title is the card's link to the detail route, stretched over the whole
  // card by `.tlink::after`. That keeps ONE anchor with real link text (a screen
  // reader hears the base name, not "link") while the chips and the action
  // buttons stay clickable above it, and it avoids nesting buttons inside an
  // anchor, which is invalid and navigates on a chip click in some browsers.
  import { base } from '$app/paths';
  import { bases, openPreview, setTag } from '$lib/bases.svelte.js';

  let { listing, linked = false, onImport } = $props();

  function fmt(n) {
    const v = Number(n) || 0;
    return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k' : String(v);
  }
  function preview() {
    openPreview({ publishId: listing.publish_id, title: listing.title, thumbUrl: listing.thumb_url });
  }

  // Chips filter the gallery in place. The click must not reach the stretched
  // title link sitting under the card.
  function pickTag(e, t) {
    e.stopPropagation();
    setTag(t);
  }

  // Short age, falling back to the plain date past a month. created_at arrives
  // as a UTC sqlite stamp with no zone marker, so normalise the separator before
  // parsing (a few hours' skew is irrelevant at day granularity).
  function when(iso) {
    if (!iso) return '';
    const t = Date.parse(String(iso).replace(' ', 'T'));
    if (Number.isNaN(t)) return '';
    const days = Math.floor((Date.now() - t) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 30) return `${days}d ago`;
    return new Date(t).toISOString().slice(0, 10);
  }

  // Only counts the payload supplies and that are non-zero; a null or 0 renders nothing.
  let census = $derived([
    { label: 'structural', value: listing.instance_count },
    { label: 'placeables', value: listing.placeable_count },
    { label: 'pentashields', value: listing.pentashield_count },
  ].filter((c) => c.value));   // zero counts are noise on most cards
  let added = $derived(when(listing.created_at));
  // 'neutral' is _card()'s stand-in for "unset", so it earns no chip.
  let faction = $derived(listing.faction && listing.faction !== 'neutral' ? listing.faction : '');

  let purpose = $derived(listing.user_tags || []);
  // Purpose tags the player chose lead, then the server-derived ones. `tags`
  // already carries the purposes merged in front (blueprint_market._decode), so
  // the Set is what keeps them from rendering twice.
  let chips = $derived([...new Set([...purpose, ...(listing.tags || [])])].slice(0, 4));
  let detailHref = $derived(`${base}/bases/${listing.publish_id}`);
</script>

<article class="card" data-faction={listing.faction || 'neutral'}>
  <div class="art" class:empty={!listing.thumb_url}>
    {#if listing.thumb_url}
      <img class="shot" src={listing.thumb_url} alt="" loading="lazy" />
    {:else}
      <span class="noart mono">no preview</span>
    {/if}
    {#if listing.has_paid_pieces}<span class="mtx mono">MTX</span>{/if}
  </div>

  <div class="body">
    <h3 class="title" title={listing.title}>
      <a class="tlink" href={detailHref}><span class="ttext">{listing.title || 'Untitled base'}</span></a>
    </h3>
    <div class="meta mono">
      <span class="author">by {listing.author_name || 'a Fremen builder'}</span>
      <span class="stats">
        <span>{fmt(listing.piece_count)} pcs</span>
        <span title="Downloads">&darr; {fmt(listing.download_count)}</span>
      </span>
    </div>
    {#if census.length || added}
      <div class="census mono">
        {#each census as c (c.label)}
          <span class="cstat"><b>{fmt(c.value)}</b> {c.label}</span>
        {/each}
        {#if added}<time class="added" datetime={listing.created_at}>{added}</time>{/if}
      </div>
    {/if}
    {#if faction || chips.length}
      <div class="tags">
        {#if faction}<span class="chip fac">{faction}</span>{/if}
        {#each chips as t (t)}
          <button
            class="chip" class:purpose={purpose.includes(t)}
            type="button" onclick={(e) => pickTag(e, t)}
            aria-label={`Filter the market by ${t}`}
          >{t}</button>
        {/each}
      </div>
    {/if}
    <div class="actions">
      <button class="btn" type="button" onclick={preview}>Preview 3D</button>
      {#if linked && bases.flags.import_enabled}
        <button class="btn ghost" type="button" onclick={() => onImport?.(listing)}>Save</button>
      {/if}
    </div>
  </div>
</article>

<style>
  .card {
    position: relative;
    display: flex; flex-direction: column;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    overflow: hidden; transition: border-color var(--motion-fast) var(--ease-out), transform var(--motion-fast) var(--ease-out);
  }
  .card:hover { transform: translateY(-2px); border-color: var(--edge-hi); }
  .art { position: relative; aspect-ratio: 16 / 10; background: var(--bg-deep); display: flex; align-items: center; justify-content: center; }
  .card[data-faction='atreides'] .art { box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--ls-ibad) 60%, transparent); }
  .card[data-faction='harkonnen'] .art { box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--ls-red) 60%, transparent); }
  .shot { width: 100%; height: 100%; object-fit: cover; }
  .noart { color: var(--text-muted); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em; }
  .mtx { position: absolute; top: 6px; right: 6px; font-size: 10px; color: var(--bg-deep); background: var(--accent-bright); border-radius: var(--radius-sm); padding: 1px var(--space-1); }
  .body { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3); }
  .title { margin: 0; font-size: var(--text-base); color: var(--text); min-width: 0; }
  .tlink { display: block; color: inherit; text-decoration: none; }
  .tlink:hover { color: var(--accent-text); }
  /* The ellipsis lives on the inner span: `overflow: hidden` on the anchor would
     clip its own stretched pseudo-element and kill the card-wide hit area. */
  .ttext { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* The whole card is the hit area; the chips and buttons below outrank it. */
  .tlink::after { content: ''; position: absolute; inset: 0; z-index: 1; }
  .tlink:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .meta { display: flex; justify-content: space-between; gap: var(--space-2); font-size: var(--text-xs); color: var(--text-muted); }
  .stats { display: flex; gap: var(--space-2); }
  .census { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-1) var(--space-2); font-size: 10px; color: var(--text-muted); }
  .cstat b { font-weight: 400; color: var(--text); }
  .added { margin-left: auto; color: var(--text-muted); }
  .tags { position: relative; z-index: 2; display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .chip { font-family: inherit; font-size: 10px; color: var(--text-muted); background: none; border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 1px var(--space-1); }
  button.chip { cursor: pointer; }
  button.chip:hover { color: var(--text); border-color: var(--accent); }
  button.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chip.purpose { color: var(--text); border-color: var(--edge-hi); }
  .chip.fac { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 50%, var(--edge)); text-transform: capitalize; }
  .actions { position: relative; z-index: 2; display: flex; gap: var(--space-2); margin-top: var(--space-1); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); cursor: pointer; }
  .btn.ghost { color: var(--text); background: var(--metal-1); border-color: var(--edge); }
  .btn:hover { filter: brightness(1.08); }
  .btn.ghost:hover { border-color: var(--accent); filter: none; }
</style>
