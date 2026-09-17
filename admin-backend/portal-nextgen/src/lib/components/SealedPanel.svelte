<script>
  // SealedPanel: the four-state read surface. The state machine is
  // guild/GuildInvitesPanel.svelte's (loading skeleton, error, empty, content)
  // and the presentation is the route-level `.sealed-panel` block that 10 routes
  // carry a copy of.
  //
  // Copy is ALWAYS caller-supplied. There are no default sentences here, because
  // an invented one is a fabricated statement about the player's data; an empty
  // list stays sealed rather than growing a placeholder row.
  //
  // The login action's `data-sveltekit-reload` is load-bearing: a client-side nav
  // would keep the SPA router in charge and break the OAuth redirect.
  //
  // `art` is a BARE PLATE NAME, never a URL: the component owns the directory and
  // the cache-bust key, so a re-cut plate is one edit here rather than a hunt
  // through twenty call sites. It renders ONLY on an `empty`, and NEVER behind an
  // `action="login"` gate: a signed-out visitor is being asked to do something,
  // and decoration behind the ask is noise in the way of it. Error branches take
  // no plate either, for the same reason.
  import { base } from '$app/paths';
  import { LOGIN_URL } from '$lib/auth.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import { detectQuality } from '$lib/quality.js';

  let {
    status = 'ready', // 'loading' | 'error' | 'empty' | 'ready'
    loadingText = 'loading',
    errorText = '',
    emptyText = '',
    action = 'none', // 'none' | 'login'
    width = 'auto',  // 'auto' | 'prose' (58ch, the karum measure)
    slab = false,
    art = null,      // bare plate name, e.g. 'no-orders'; see static/img/v2/empty/
    children,
  } = $props();

  const ART_DIR = '/img/v2/empty/';
  // Bumped whenever a plate is re-cut. One key for the whole set: they ship and
  // change together, and a per-file key is a per-file thing to forget.
  const ART_VERSION = '20260903a';

  // Data saver is read from the one quality detector the app already has; a
  // second ad-hoc `navigator.connection` probe here would be a second thing to
  // keep true. SSR-safe: detectQuality returns saveData:false without a window.
  const saveData = detectQuality().saveData;

  let plate = $derived(
    art && status === 'empty' && action !== 'login' && !saveData
      ? `${base}${ART_DIR}${art}.webp?v=${ART_VERSION}`
      : null
  );
</script>

{#snippet body()}
  {#if status === 'loading'}
    <p class="skeleton">{loadingText}</p>
  {:else if status === 'error'}
    <p class="sealed" class:prose={width === 'prose'}>{errorText}</p>
  {:else if status === 'empty'}
    {#if plate}
      <img class="plate" src={plate} alt="" aria-hidden="true" loading="lazy" decoding="async" />
    {/if}
    <p class="sealed" class:prose={width === 'prose'}>{emptyText}</p>
    {#if action === 'login'}
      <a class="login" href={LOGIN_URL} data-sveltekit-reload>Sign in</a>
    {/if}
  {:else}
    {@render children?.()}
  {/if}
{/snippet}

{#if slab}
  <CarvedSlab>
    <div class="inner" class:plated={!!plate}>{@render body()}</div>
  </CarvedSlab>
{:else}
  <section class="panel inner" class:plated={!!plate}>{@render body()}</section>
{/if}

<style>
  .inner {
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-4); flex-wrap: wrap;
  }
  .panel {
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-lg); padding: var(--space-5);
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 18px 44px -28px var(--shadow-cast);
  }
  /* Plated empty: the plate sits behind the copy, the copy stays on top and
     keeps its own contrast. Only the plated variant becomes a positioning
     context, so an unplated panel's layout is byte-identical to what shipped. */
  .inner.plated { position: relative; overflow: hidden; min-height: 128px; }
  .inner.plated .sealed { position: relative; z-index: 1; }
  .plate {
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: contain; opacity: .38; z-index: 0; pointer-events: none;
  }
  /* Below 420px the plate would sit under the sentence rather than behind it. */
  @media (max-width: 420px) { .plate { display: none; } }

  .sealed { color: var(--text-muted); font-size: var(--text-sm); margin: 0; line-height: 1.45; }
  /* A long seal sentence sets to a readable measure instead of running the full
     panel width; the karum stall carried this cap locally before the primitive. */
  .sealed.prose { max-width: 58ch; }
  .login {
    text-decoration: none; color: var(--bg-deep);
    background: var(--accent); border: 1px solid var(--accent);
    border-radius: var(--radius-sm); font-family: var(--font-display);
    letter-spacing: .06em; font-size: var(--text-sm);
    padding: var(--space-1) var(--space-4);
    box-shadow: 0 0 10px var(--accent-glow); transition: filter var(--motion-fast) var(--ease-out);
  }
  .login:hover { filter: brightness(1.12); }
</style>
