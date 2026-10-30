<script>
  // Title and map dot picker (COSMETICS-MAP-ICONS-PLAN P0.3). It lives here, not
  // only on the Seven Nights page, because that page's nav closes with the event
  // and a bought title must stay wearable after it.
  //
  // GET /portal/cosmetics lists only what the caller owns and answers
  // enabled:false while LASTSIETCH_COSMETIC_TITLES_ENABLED is dark; then nothing
  // renders. A pick is the existing prefs PUT, which re-checks ownership on the
  // server. The local prefs doc is patched on success so the map picks up a new
  // dot without a reload.
  import { onMount } from 'svelte';
  import { getJSON, api } from '$lib/api.js';
  import { prefs } from '$lib/prefs.svelte.js';
  import TitleChip from '$lib/components/TitleChip.svelte';

  let view = $state(null);
  let notice = $state('');
  let busy = $state(false);

  let titles = $derived(view?.titles || []);
  let mapdots = $derived(view?.mapdots || []);
  let shown = $derived(view?.enabled === true && (titles.length > 0 || mapdots.length > 0));
  let worn = $derived(titles.find((t) => t.key === view?.equipped?.flair) || null);

  onMount(async () => {
    try {
      view = await getJSON('/portal/cosmetics');
    } catch (e) {
      view = null;
    }
  });

  async function pick(kind, key) {
    const pref = kind === 'flair' ? 'sietch_flair' : 'sietch_mapdot';
    const prior = view.equipped[kind];
    view.equipped[kind] = key;
    busy = true;
    notice = '';
    try {
      await api.settings.prefs.set('', { [pref]: key });
      const next = { ...prefs.identity };
      if (key === null) delete next[pref];
      else next[pref] = key;
      prefs.identity = next;
      notice = 'Saved.';
    } catch (e) {
      view.equipped[kind] = prior;
      notice = 'That could not be saved. Try again in a moment.';
    } finally {
      busy = false;
    }
  }
</script>

{#if shown}
  <div class="cosmetics">
    <p class="lbl mono">Cosmetics</p>
    {#if titles.length}
      <fieldset class="group" disabled={busy}>
        <legend class="sub mono">Title</legend>
        <label class="opt"><input type="radio" name="cos-title" checked={!view.equipped.flair} onchange={() => pick('flair', null)} /> None</label>
        {#each titles as t (t.key)}
          <label class="opt"><input type="radio" name="cos-title" checked={view.equipped.flair === t.key} onchange={() => pick('flair', t.key)} /> {t.name}</label>
        {/each}
        <p class="explain">Shown beside your name on Standings and in portal chat.{#if worn} Preview: <span class="preview">You<TitleChip title={worn} /></span>{/if}</p>
      </fieldset>
    {/if}
    {#if mapdots.length}
      <fieldset class="group" disabled={busy}>
        <legend class="sub mono">Map dot</legend>
        <label class="opt"><input type="radio" name="cos-mapdot" checked={!view.equipped.mapdot} onchange={() => pick('mapdot', null)} /> Default</label>
        {#each mapdots as m (m.key)}
          <label class="opt"><input type="radio" name="cos-mapdot" checked={view.equipped.mapdot === m.key} onchange={() => pick('mapdot', m.key)} /> {m.name}</label>
        {/each}
        <p class="explain">Your own dot on the portal maps. Only you see it.</p>
      </fieldset>
    {/if}
    {#if notice}<p class="notice mono" role="status">{notice}</p>{/if}
  </div>
{/if}

<style>
  .cosmetics { margin-top: var(--space-4); padding-top: var(--space-3); border-top: 1px solid var(--border-subtle); }
  .lbl { margin: 0; font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .group { margin: var(--space-3) 0 0; padding: 0; border: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .sub { padding: 0; margin-bottom: var(--space-1); font-size: var(--text-xs); color: var(--accent-text); letter-spacing: .1em; text-transform: uppercase; }
  .opt { display: flex; align-items: center; gap: var(--space-2); min-height: 32px; font-size: var(--text-sm); color: var(--text); cursor: pointer; }
  .opt input { width: 18px; height: 18px; margin: 0; accent-color: var(--accent); }
  .explain { margin: var(--space-1) 0 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
  .preview { color: var(--text); white-space: nowrap; }
  .notice { margin: var(--space-2) 0 0; font-size: var(--text-xs); color: var(--text-muted); }
</style>
