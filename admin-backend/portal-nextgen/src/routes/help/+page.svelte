<script>
  // Help: what changed in each release, and how the portal works.
  //
  // PUBLIC. No auth gate, no sealed panel, no session read. Half of what this
  // page explains is how to get an account linked in the first place, so a gate
  // here would seal the manual against exactly the player who needs it.
  //
  // Every sentence lives in $lib/help/content.js, which is plain data with the
  // source file for each fact named above it. Nothing is typed into this
  // template: a fact in the markup is a second copy that drifts the day the
  // behaviour changes, and this page is read precisely by the players who would
  // then be misled by it.
  //
  // The ONE read is the public feature list. Entries that describe a feature
  // still behind its gate stay hidden until it answers true, and a read that is
  // pending, refused or malformed leaves them hidden as well: sending a player
  // to look for a button nobody has is worse than a page one entry short.
  import { onMount } from 'svelte';
  import { version } from '$app/environment';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import { api } from '$lib/api.js';
  import { WHATS_NEW, HOW_TO, visible } from '$lib/help/content.js';

  let features = $state(null);

  // A version whose every line is feature-keyed and dark drops out entirely
  // rather than printing a heading with nothing under it.
  let releases = $derived(
    WHATS_NEW
      .map((rel) => ({ ...rel, items: visible(rel.items, features) }))
      .filter((rel) => rel.items.length > 0)
  );
  let guides = $derived(visible(HOW_TO, features));

  onMount(async () => {
    try {
      const r = await api.server.features();
      features = r && typeof r.features === 'object' ? r.features : null;
    } catch (e) {
      features = null;
    }
  });
</script>

<svelte:head>
  <title>Help | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | field manual"
    title="Help"
    sub="What changed in each release of the companion, and how the parts of it work."
  />

  <section class="block" aria-labelledby="whatsnew-h">
    <CarvedSlab>
      <p class="cap mono">Release notes</p>
      <h2 class="gname" id="whatsnew-h">Server and Portal Changelog</h2>
      <p class="intro">Newest first. The build you are reading this on is marked. Older releases are further down the box.</p>
      <div class="rels-box" tabindex="0" role="region" aria-label="Server and Portal Changelog">
        <ol class="rels" role="list">
          {#each releases as rel (rel.version)}
            <li class="rel">
              <p class="relhead">
                <span class="ver mono">v{rel.version}</span>
                {#if rel.version === version}<span class="now mono">this release</span>{/if}
                <span class="when mono">{rel.date}</span>
              </p>
              <h3 class="reltitle">{rel.title}</h3>
              <ul class="lines" role="list">
                {#each rel.items as item (item.text)}
                  <li>{item.text}</li>
                {/each}
              </ul>
            </li>
          {/each}
        </ol>
      </div>
    </CarvedSlab>
  </section>

  <section class="block" aria-labelledby="howto-h">
    <h2 class="gname" id="howto-h">How to</h2>
    <nav class="jump" aria-label="How to entries">
      <ul role="list">
        {#each guides as g (g.id)}
          <li><a href="#howto-{g.id}">{g.title}</a></li>
        {/each}
      </ul>
    </nav>

    <div class="guides">
      {#each guides as g (g.id)}
        <section class="guide" id="howto-{g.id}">
          <CarvedSlab>
            <p class="cap mono">In the portal since v{g.since}</p>
            <h3 class="gtitle">{g.title}</h3>
            {#each g.body as para}
              <p class="para">{para}</p>
            {/each}
            {#if g.steps}
              <ol class="steps">
                {#each g.steps as step}
                  <li class="mono">{step}</li>
                {/each}
              </ol>
            {/if}
          </CarvedSlab>
        </section>
      {/each}
    </div>
  </section>
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .block { margin-bottom: var(--space-6); }

  /* Panel caption, not a page header: PageHeader owns the one kicker on the
     route and this is the 34th of the in-panel captions the app already has. */
  .cap { color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); margin: 0 0 var(--space-2); }
  .gname {
    margin: 0 0 var(--space-4); font-size: var(--text-lg);
    text-transform: uppercase; letter-spacing: .06em; color: var(--accent-bright);
  }
  .intro { margin: 0 0 var(--space-4); font-size: var(--text-sm); color: var(--text-muted); max-width: 62ch; line-height: 1.55; }

  /* A fixed box you scroll, not a page that grows one release longer every
     week: the How to section below stays reachable without paging past the whole
     history. The box is a wrapper rather than the list itself, because a role on
     the <ol> would take the list semantics off it and orphan every <li> inside.
     tabindex makes the box a keyboard stop, which is what lets somebody without a
     mouse scroll it at all; overscroll-behavior keeps that scroll from running on
     into the page once the list bottoms out. */
  .rels-box {
    max-height: 26rem; overflow-y: auto;
    overscroll-behavior: contain; scrollbar-gutter: stable;
    padding-right: var(--space-3);
  }
  .rels-box:focus-visible { outline: 1px solid var(--edge-hi); outline-offset: var(--space-1); }
  .rels { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-5); }
  .rel { border-left: 2px solid var(--edge); padding-left: var(--space-4); }
  .relhead { margin: 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-2); }
  /* A version number is chrome, not streaming data, so it stays amber. */
  .ver { font-size: var(--text-sm); color: var(--accent-text); }
  .now {
    font-size: 10px; text-transform: uppercase; letter-spacing: .12em;
    color: var(--accent-bright); border: 1px solid var(--edge-hi);
    border-radius: 999px; padding: 1px var(--space-2);
  }
  .when { font-size: var(--text-xs); color: var(--text-muted); opacity: .8; }
  .reltitle { margin: var(--space-1) 0 var(--space-2); font-size: var(--text-base); color: var(--text); letter-spacing: .01em; }
  .lines { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .lines li { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.55; max-width: 68ch; }
  .lines li::before { content: '\25C6'; color: var(--accent); font-size: 9px; margin-right: var(--space-2); vertical-align: middle; }

  .jump { margin: 0 0 var(--space-5); }
  .jump ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .jump a {
    display: inline-block; font-size: var(--text-xs); color: var(--text-muted);
    text-decoration: none; border: 1px solid var(--edge); border-radius: 999px;
    padding: var(--space-1) var(--space-3); background: var(--metal-0);
  }
  .jump a:hover, .jump a:focus-visible { color: var(--accent-bright); border-color: var(--edge-hi); }

  .guides { display: flex; flex-direction: column; gap: var(--space-4); }
  /* scroll-margin so a jump link does not park the heading under the sticky bar. */
  .guide { scroll-margin-top: calc(var(--space-8) + var(--space-6)); }
  .gtitle { margin: 0 0 var(--space-3); font-size: var(--text-base); color: var(--accent-text); text-transform: uppercase; letter-spacing: .05em; }
  .para { margin: 0 0 var(--space-3); font-size: var(--text-sm); color: var(--text); line-height: 1.6; max-width: 70ch; }
  .para:last-of-type { margin-bottom: 0; }
  .steps { margin: var(--space-3) 0 0; padding: 0 0 0 1.4em; display: flex; flex-direction: column; gap: var(--space-1); max-width: 70ch; }
  .steps li { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.55; }
  .steps li::marker { color: var(--accent); }

  @media (max-width: 40rem) {
    .rel { padding-left: var(--space-3); }
  }
</style>
