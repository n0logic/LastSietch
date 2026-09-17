<script>
  // One sietch, deep-linkable at /guilds/<id>. The Signal Board could
  // show a guild but never open one, so a sietch had no page to link, share or
  // read a roster from.
  //
  // LINKED SESSION ONLY (ruling 4). The directory this reads is session-gated
  // server-side; a signed-out visitor gets the login seal and no request is made.
  // A public variant is wave 10.
  //
  // Three things are deliberate:
  //   * The id is validated against a bounded digits pattern BEFORE any request.
  //     A junk or enormous id never becomes a lookup and never reaches copy.
  //   * A miss seals to one panel that says nothing about WHICH id was asked for.
  //     Left, disbanded, never existed and "not visible to you" are one answer,
  //     because telling them apart is exactly what a probe wants.
  //   * The roster prints CHARACTER NAMES and role names only. account_id is
  //     never in this payload and player_controller_id is not rendered: those are
  //     for member ops on the hub, not for a page anyone in the sietch can open.
  //
  // RosterToken is deliberately NOT reused here. It renders a presence dot and a
  // last-seen caption, and /portal/guilds/data carries no census, so every row
  // would read "offline" as a fabricated claim.
  import { untrack } from 'svelte';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { api } from '$lib/api.js';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import RecruitingBeacon from '$lib/components/guild/RecruitingBeacon.svelte';

  const gate = useAuthGate();

  // A guild id is a plain positive integer, bounded so a giant string is a
  // client-side refusal rather than a request the backend has to reject.
  let id = $derived(
    /^[0-9]{1,12}$/.test(String($page.params.id || '')) ? String($page.params.id) : ''
  );

  let guilds = $state([]);
  let status = $state('idle'); // idle | loading | ready | error

  let guild = $derived(
    status === 'ready'
      ? (guilds.find((g) => String(g?.guild_id ?? '') === id) || null)
      : null
  );

  let phase = $derived(
    !id ? 'missing'
      : status === 'ready' ? (guild ? 'ready' : 'missing')
      : status === 'error' ? 'error'
      : 'loading'
  );

  let name = $derived(String(guild?.name || guild?.guild_name || '').trim());
  let faction = $derived(String(guild?.faction || '').trim());
  let memberCount = $derived(
    typeof guild?.member_count === 'number' ? guild.member_count : null
  );
  let description = $derived(String(guild?.description || '').trim());
  let rec = $derived(guild?.recruiting || null);
  let recruiting = $derived(rec?.open === true || rec?.open === 1);
  let blurb = $derived(String(rec?.message || '').trim());
  let rankOverall = $derived(
    typeof guild?.rank_overall === 'number' ? guild.rank_overall : null
  );
  let rankSession = $derived(
    typeof guild?.rank_session === 'number' ? guild.rank_session : null
  );
  let members = $derived(Array.isArray(guild?.members) ? guild.members : []);

  // Render-side scheme guard (the hub carries the same one): only an explicit
  // https:// URL becomes a link. Anything else is omitted rather than cleaned up.
  let safeDiscord = $derived(
    typeof rec?.discord_url === 'string' && /^https:\/\//i.test(rec.discord_url.trim())
      ? rec.discord_url.trim()
      : ''
  );

  async function load() {
    status = 'loading';
    try {
      const r = await api.guilds.data();
      const rows = Array.isArray(r) ? r : (r?.guilds ?? []);
      guilds = Array.isArray(rows) ? rows : [];
      status = 'ready';
    } catch (e) {
      guilds = [];
      status = 'error';
    }
  }

  let loaded = false;
  $effect(() => {
    const s = gate.status;
    if (s === 'authed' && id && !loaded) { loaded = true; untrack(load); }
    else if (s === 'anon') { loaded = false; guilds = []; status = 'idle'; }
  });
</script>

<svelte:head>
  <title>{name ? `${name} | Last Sietch` : 'Sietch | Last Sietch'}</title>
</svelte:head>

<div class="page">
  <PageHeader kicker="Last Sietch | water-bond registry" title={name || 'Sietch'}>
    <a class="back mono" href="{base}/guilds">Back to the registry</a>
  </PageHeader>

  {#if gate.loading}
    <SealedPanel status="loading" loadingText="loading" slab={true} />
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login" width="prose"
      emptyText="Sign in to read a guild page. The registry is open to bonded players only."
    />
  {:else if phase === 'loading' || phase === 'idle'}
    <SealedPanel status="loading" loadingText="loading" slab={true} />
  {:else if phase === 'error'}
    <SealedPanel
      status="error" slab={true} width="prose"
      errorText="The registry could not be read right now. Try again shortly."
    />
  {:else if phase === 'missing'}
    <SealedPanel
      status="empty" action="none" slab={true} width="prose"
      emptyText="That guild is not in the registry. It may have disbanded, or the link may be out of date. The registry is still there."
    />
  {:else if guild}
    <CarvedSlab>
      <div class="head">
        <div class="titles">
          <h2 class="gname">{name || 'Unnamed guild'}</h2>
          {#if faction}<span class="faction mono">{faction}</span>{/if}
        </div>
        {#if recruiting}<RecruitingBeacon />{/if}
      </div>

      <dl class="facts mono">
        {#if memberCount !== null}
          <div><dt>members</dt><dd>{memberCount}</dd></div>
        {/if}
        {#if rankOverall !== null}
          <div><dt>rank, overall</dt><dd>{rankOverall}</dd></div>
        {/if}
        {#if rankSession !== null}
          <div><dt>rank, session</dt><dd>{rankSession}</dd></div>
        {/if}
      </dl>

      {#if description}<p class="desc">{description}</p>{/if}

      {#if recruiting}
        <section class="rec">
          <p class="kicker mono">Recruiting</p>
          {#if blurb}<p class="desc">{blurb}</p>{/if}
          {#if rec?.playstyle || rec?.timezone || rec?.language || rec?.new_player_friendly}
            <div class="tags">
              {#if rec?.playstyle}<span class="tag mono">{rec.playstyle}</span>{/if}
              {#if rec?.timezone}<span class="tag mono">{rec.timezone}</span>{/if}
              {#if rec?.language}<span class="tag mono">{rec.language}</span>{/if}
              {#if rec?.new_player_friendly}<span class="tag mono np">new-player friendly</span>{/if}
            </div>
          {/if}
          {#if safeDiscord}
            <a class="discord mono" href={safeDiscord} target="_blank" rel="noopener noreferrer">Discord</a>
          {/if}
        </section>
      {/if}

      <section class="roster">
        <p class="kicker mono">Roster</p>
        {#if members.length === 0}
          <SealedPanel
            status="empty" action="none" width="prose"
            emptyText="No roster is listed for this guild."
          />
        {:else}
          <ul class="members">
            {#each members as m, i (i)}
              <li class="member">
                <span class="mname">{m?.character_name || m?.char_name || 'Unnamed Fremen'}</span>
                {#if m?.role_name}<span class="mrole mono">{m.role_name}</span>{/if}
                {#if m?.is_self === true}<span class="you mono">you</span>{/if}
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    </CarvedSlab>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .back {
    display: inline-block; font-size: var(--text-xs); letter-spacing: .1em;
    text-transform: uppercase; color: var(--text-muted); text-decoration: none;
  }
  .back:hover { color: var(--accent-text); }

  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
  .titles { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .gname { font-size: var(--text-xl); letter-spacing: .04em; text-transform: uppercase; }
  .faction { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .1em; text-transform: uppercase; }

  .facts { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-5); margin: var(--space-4) 0 0; font-size: var(--text-xs); }
  .facts div { display: flex; flex-direction: column; gap: 2px; }
  .facts dt { color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; font-size: 10px; }
  .facts dd { margin: 0; color: var(--text); }

  .desc { margin: var(--space-4) 0 0; font-size: var(--text-sm); line-height: 1.55; color: var(--text-muted); white-space: pre-wrap; overflow-wrap: anywhere; max-width: 68ch; }

  .kicker { margin: 0 0 var(--space-2); color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .rec, .roster { margin-top: var(--space-5); }
  .rec .desc { margin-top: 0; }

  .tags { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-3); }
  .tag {
    font-size: var(--text-xs); letter-spacing: .06em; color: var(--text-muted);
    padding: var(--space-1) var(--space-2);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .tag.np { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 40%, var(--edge)); }
  .discord {
    display: inline-block; margin-top: var(--space-3);
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    color: var(--accent-text); text-decoration: none;
    padding: var(--space-1) var(--space-3); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .discord:hover { border-color: var(--accent); }

  .members { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .member {
    display: inline-flex; align-items: center; gap: var(--space-2); max-width: 100%;
    padding: var(--space-1) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .mname { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mrole { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .08em; text-transform: uppercase; }
  .you { font-size: var(--text-xs); color: var(--accent-text); letter-spacing: .12em; text-transform: uppercase; }
</style>
