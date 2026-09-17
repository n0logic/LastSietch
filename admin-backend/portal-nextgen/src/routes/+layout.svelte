<script>
  import '../app.css';
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { version } from '$app/environment';
  import { page } from '$app/stores';
  import { auth, loadAuth, logout, LOGIN_URL } from '$lib/auth.svelte.js';
  import { loadPrefs } from '$lib/prefs.svelte.js';
  import { api } from '$lib/api.js';
  import { mailbox, refreshUnread } from '$lib/mailbox.svelte.js';
  import { exchange, refreshAlerts } from '$lib/exchange.svelte.js';
  import { sendSolari, closeSendSolari } from '$lib/sendSolari.svelte.js';
  import BrandMark from '$lib/components/BrandMark.svelte';
  import ServerSchedule from '$lib/components/ServerSchedule.svelte';
  import SendSolariDialog from '$lib/components/SendSolariDialog.svelte';
  import TabBar from '$lib/components/TabBar.svelte';
  import Reach from '$lib/reach/Reach.svelte';
  let { children } = $props();

  // Primary nav, grouped into the five IA clusters (wave 4). The grouping is the
  // only change: every route keeps its URL and its place in the bar. `v2:true` =
  // rebuilt in this app (client routing); anything else deep-links to the V1 page
  // with a full navigation. Server folded into the V2 Dashboard (the Sietch
  // Weather Station), so it is no longer a tab; the V1 /portal/server page stays
  // live as the V1 fallback only.
  const NAV = [
    // Mailbox is intentionally NOT in the primary nav; it stays reachable via the
    // bell icon in the top bar. Settings likewise lives on the gear beside it.
    // Help is not a section either: it is the question mark beside the gear in
    // the top bar (owner ruling 2026-09-06) plus the footer link, and its first
    // section is what changed in the release the footer pill names.
    { group: 'Home', items: [
      { label: 'Dashboard', href: `${base}/`, v2: true },
      { label: 'Rewards', href: `${base}/rewards`, v2: true },
      { label: 'Activity', href: `${base}/activity`, v2: true },
      { label: 'First session', href: `${base}/start`, v2: true },
    ] },
    // `hub` (wave 5, Desert and Economy only): the group label is itself a route,
    // so the cluster name becomes the way into the section's own landing page.
    // A group without `hub` keeps the unclickable chrome label it shipped with.
    { group: 'Desert', hub: `${base}/desert`, items: [
      { label: 'Maps', href: `${base}/maps`, v2: true },
    ] },
    // The Karum sits next to Exchange on purpose: they are the two places you trade,
    // and the whole pitch of the Karum is that it is the OTHER one.
    { group: 'Economy', hub: `${base}/economy`, items: [
      { label: 'Exchange', href: `${base}/exchange`, v2: true, badge: 'alerts' },
      { label: 'Karum', href: `${base}/karum`, v2: true },
    ] },
    // Solido sits last in Holdings: it is a utility you visit deliberately, not a
    // place you browse. The V1 /portal/solido gallery stays live alongside it.
    { group: 'Holdings', items: [
      { label: 'Character', href: `${base}/character`, v2: true },
      { label: 'Storage', href: `${base}/storage`, v2: true },
      { label: 'Bases', href: `${base}/bases`, v2: true },
      { label: 'Solido', href: `${base}/solido`, v2: true },
    ] },
    // Chat sits next to Guilds (wave 11): it is the other place you talk to the
    // sietch, and its guild room is the one a guild member reaches for first.
    // Mailbox stays off the bar on the bell, so this is as close to "between
    // Guilds and Mailbox" as the grouped IA gets.
    { group: 'Sietch', items: [
      { label: 'Guilds', href: `${base}/guilds`, v2: true },
      { label: 'Chat', href: `${base}/chat`, v2: true },
      { label: 'Events', href: `${base}/events`, v2: true },
      { label: 'Reports', href: `${base}/reports`, v2: true, feature: 'reports' },
      { label: 'Landsraad', href: `${base}/landsraad`, v2: true },
    ] },
  ];
  let reportsEnabled = $state(false);
  let visibleNav = $derived(NAV.map((section) => ({ ...section, items: section.items.filter((item) => item.feature !== 'reports' || reportsEnabled) })));
  let path = $derived($page.url.pathname);
  function navActive(item) {
    if (!item.v2) return false;
    // Dashboard is active on the app home only.
    if (item.href === `${base}/`) return path === base || path === `${base}/`;
    return path.startsWith(item.href);
  }
  // A hub label lights on its own page ALONE. `startsWith` would be wrong here:
  // the sub-links live under their own paths, and a label that stayed lit across
  // the whole section would read as two current tabs at once.
  function hubActive(section) {
    return !!section.hub && (path === section.hub || path === `${section.hub}/`);
  }
  let activeSection = $derived(visibleNav.find((section) => hubActive(section) || section.items.some(navActive)));

  // Identity headline, read-only since wave 4: the switchers moved to Settings, so
  // the bar reads out the character in force (falls back to the account's default
  // pick, then to the linked set for a fresh load before /portal/characters
  // resolves).
  const selectedChar = $derived(
    auth.characters.find((c) => c.selected) || auth.characters[0] || null
  );
  const activeChar = $derived(
    selectedChar?.char_name || auth.linked[0]?.character_name || ''
  );

  // Server status pill: reassures players the server is up (the 2026-07-17
  // outage caused a panic because the portal gave no signal). Driven by the
  // existing public /api/dune/status; any failure degrades to 'unknown' (no
  // pill), never a false "offline".
  let serverStatus = $state('unknown'); // 'online' | 'offline' | 'unknown'
  async function pollStatus() {
    try {
      const s = await api.server.duneStatus();
      const maps = Array.isArray(s?.maps) ? s.maps.length : 0;
      serverStatus = (s && s.available && maps > 0) ? 'online' : 'offline';
    } catch (e) {
      serverStatus = 'unknown';
    }
  }

  // Theme + house moved to Settings, which only WRITES ls-theme / ls-house. The
  // shell keeps applying them on boot so a stored skin survives a cold load
  // (app.html does the same pre-paint, to avoid a flash of the default skin).
  function applyStoredAppearance() {
    const el = document.documentElement;
    try {
      const t = localStorage.getItem('ls-theme');
      if (t) el.setAttribute('data-theme', t);
      const h = localStorage.getItem('ls-house');
      if (h) el.setAttribute('data-house', h);
    } catch (e) {}
  }

  onMount(() => {
    applyStoredAppearance();
    api.server.features().then((data) => { reportsEnabled = data?.features?.reports === true; }).catch(() => { reportsEnabled = false; });
    // Preferences read after the session is known: the mirror paints first,
    // the account doc replaces it once /portal/me has answered.
    loadAuth().then(() => loadPrefs());
    pollStatus();
    const iv = setInterval(pollStatus, 45000);
    return () => clearInterval(iv);
  });

  // Pull the unread-mail count once the viewer is known (bell badge). Anon/failure
  // degrades to 0 inside the store; nothing is fabricated for a signed-out user.
  let unreadLoaded = false;
  $effect(() => {
    if (auth.status === 'authed' && !unreadLoaded) { unreadLoaded = true; refreshUnread(); }
    else if (auth.status === 'anon') { unreadLoaded = false; }
  });

  // Same shape for the Exchange nav badge: FIRED price alerts the player has not
  // looked at yet. Never the armed-watch count -- a watch you set is not news.
  let alertsLoaded = false;
  $effect(() => {
    if (auth.status === 'authed' && !alertsLoaded) { alertsLoaded = true; refreshAlerts(); }
    else if (auth.status === 'anon') { alertsLoaded = false; }
  });
  let firedAlerts = $derived(auth.status === 'authed' ? (exchange.firedCount || 0) : 0);
</script>

<a class="skip-link" href="#main-content">Skip to content</a>
{#if import.meta.env.VITE_REVIEW_PREVIEW === 'true'}
  <aside class="review-banner"><b>Local preview</b><span>Sample data. Actions stay local.</span><a href="/?preview=habbanya" data-sveltekit-reload>Habbanya</a><a href="/?preview=amtal" data-sveltekit-reload>Amtal</a><a href="/?preview=anon" data-sveltekit-reload>Signed out</a><a href="/start?preview=unlinked" data-sveltekit-reload>Unlinked</a><a href="http://localhost:4174/dune/">Joining page</a></aside>
{/if}
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href={`${base}/`}>
      <BrandMark size={30} />
      <span class="brand-text">Last Sietch</span>
    </a>
    <Reach sections={visibleNav} />
    <div class="controls">
      {#if serverStatus !== 'unknown'}
        <span class="srv-pill {serverStatus}" title={serverStatus === 'online' ? 'Server online' : 'Server offline or in maintenance'}>
          <span class="srv-dot" aria-hidden="true"></span>
          {serverStatus === 'online' ? 'Online' : 'Offline'}
        </span>
      {/if}
      <!-- V2 runs parallel to V1 for the rollout window; an understated escape hatch
           back to the classic companion (full navigation out of the SPA). -->
      <a class="to-v1" href="/portal/account" data-sveltekit-reload title="Switch to the classic V1 companion">Classic</a>

      {#if auth.status === 'authed'}
        <div class="account">
          <a class="bell" href={`${base}/mailbox`} aria-label={mailbox.unread > 0 ? `Mailbox, ${mailbox.unread} unread` : 'Mailbox'} title="Mailbox">
            <span class="bell-glyph" aria-hidden="true">✉</span>
            {#if mailbox.unread > 0}<span class="bell-badge mono">{mailbox.unread > 99 ? '99+' : mailbox.unread}</span>{/if}
          </a>
          <span class="acct-id" title={auth.discordHandle}>
            {#if activeChar}<span class="acct-char">{activeChar}</span>{/if}
            <span class="acct-handle mono">{auth.discordHandle || 'Linked'}</span>
          </span>
          <!-- Linked accounts, the switchers, appearance, export and directory
               visibility all sit behind this one gear now. -->
          <a class="gear" href={`${base}/settings`} aria-label="Settings" title="Settings">
            <span class="gear-glyph" aria-hidden="true">⚙</span>
          </a>
          <a class="help" href={`${base}/help`} aria-label="Help" title="Help and what's new">
            <span class="help-glyph mono" aria-hidden="true">?</span>
          </a>
          <button class="acct-out" onclick={logout}>Sign out</button>
        </div>
      {:else if auth.status === 'anon'}
        <a class="acct-login" href={LOGIN_URL} data-sveltekit-reload>Sign in</a>
        <a class="help" href={`${base}/help`} aria-label="Help" title="Help and what's new">
          <span class="help-glyph mono" aria-hidden="true">?</span>
        </a>
      {:else}
        <span class="acct-loading" aria-hidden="true"></span>
      {/if}
    </div>
  </div>

  <nav class="mainnav" aria-label="Portal sections">
    <div class="mainnav-inner">
      {#each activeSection ? [activeSection] : [] as section}
        <div class="navgroup" role="group" aria-label={section.group}>
          {#if section.hub}
            <a
              class="navgroup-label"
              class:on={hubActive(section)}
              href={section.hub}
              aria-current={hubActive(section) ? 'page' : undefined}
            >{section.group}</a>
          {:else}
            <span class="navgroup-label" aria-hidden="true">{section.group}</span>
          {/if}
          <div class="navgroup-links">
            {#each section.items as item}
              <a
                class="navlink"
                class:on={navActive(item)}
                href={item.href}
                aria-current={navActive(item) ? 'page' : undefined}
                {...item.v2 ? {} : { 'data-sveltekit-reload': true }}
              >{item.label}{#if item.badge === 'alerts' && firedAlerts > 0}<span class="nav-badge mono" aria-label="{firedAlerts} price alerts fired">{firedAlerts > 99 ? '99+' : firedAlerts}</span>{/if}</a>
            {/each}
          </div>
        </div>
      {/each}
    </div>
  </nav>
  <div class="time-rail"><ServerSchedule compact /></div>
</header>

<main class="shell" id="main-content" tabindex="-1">
  {@render children?.()}
</main>

<!-- Phone width: the grouped strip above hides and the mockup's rail becomes a
     bottom tab bar (wave 4.1). Same NAV, same hrefs, same active rule. -->
<TabBar sections={visibleNav} isActive={navActive} settingsHref={`${base}/settings`} settingsActive={path.startsWith(`${base}/settings`)} {firedAlerts} />

<!-- Footer parity with the V1 shell: an unemphasised link row, nothing more.
     Ko-fi sits beside Discord as one of our links; it is deliberately not a
     call to action anywhere in the portal. -->
<footer class="ftr" role="contentinfo">
  <div class="ftr-inner">
    <span>Last Sietch · Dune: Awakening community server
      <!-- package.json version via kit.version.name; bumped with the SW key on
           every V2 deploy so a player can quote the build they are on. -->
      <a class="ftr-ver" href={`${base}/help`} title="What's new in this release">v{version}</a></span>
    <span class="ftr-links">
      <!-- Server Rules has no nav entry (owner ruling 2026-09-03): it is a
           reference page, reached from here, the Desert hub and the Station card. -->
      <a href={`${base}/rules`}>Rules</a>
      <span class="ftr-sep" aria-hidden="true">·</span>
      <a href={`${base}/help`}>Help</a>
      <span class="ftr-sep" aria-hidden="true">·</span>
      <a href="/portal/account" data-sveltekit-reload>Classic</a>
      <span class="ftr-sep" aria-hidden="true">·</span>
      <a href="https://discord.gg/your-invite" rel="noopener" target="_blank">Discord</a>
      <span class="ftr-sep" aria-hidden="true">·</span>
      <a href="https://ko-fi.com/lastsietch" rel="noopener" target="_blank">Tip Jar</a>
    </span>
  </div>
</footer>

{#if sendSolari.open}
  <SendSolariDialog onClose={closeSendSolari} />
{/if}

<style>
  .review-banner { margin-left: 104px; display: flex; align-items: center; flex-wrap: wrap; gap: 12px; padding: 8px 16px; background: #ddd0af; color: #302616; font: 12px var(--font-sans); }
  .review-banner a { color: #302616; }
  @media (max-width: 759px) { .review-banner { margin-left: 0; gap: 8px; } }
  .skip-link { position: fixed; top: 8px; left: 120px; z-index: 150; transform: translateY(-160%); padding: 12px; background: var(--bg-elevated); color: var(--text); }
  .skip-link:focus { transform: none; }
  .topbar {
    position: sticky; top: 0; z-index: 50;
    /* viewport-fit=cover: on a notched phone the status bar sits over the page, so
       the bar starts below it (live QA 2026-09-03: the clock overlapped the brand). */
    padding: calc(var(--space-3) + env(safe-area-inset-top)) 0 var(--space-3);
    background: color-mix(in srgb, var(--bg-deep) 86%, transparent);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border-subtle);
  }
  .topbar-inner {
    max-width: var(--content-max); margin: 0 auto;
    padding: 0 var(--space-4);
    display: flex; align-items: center; justify-content: space-between; gap: var(--space-3);
  }
  .brand { display: inline-flex; align-items: center; gap: var(--space-2); text-decoration: none; color: var(--text); }
  .brand-text { font-family: var(--font-display); font-weight: 700; letter-spacing: .14em; font-size: var(--text-xl); text-transform: uppercase; white-space: nowrap; }
  .srv-pill {
    display: inline-flex; align-items: center; gap: var(--space-2);
    margin-right: var(--space-1); padding: 2px var(--space-2);
    border: 1px solid var(--edge); border-radius: 999px;
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text-muted); white-space: nowrap;
  }
  .srv-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); }
  .srv-pill.online { color: var(--ls-ibad); border-color: color-mix(in srgb, var(--ls-ibad) 45%, transparent); }
  .srv-pill.online .srv-dot { background: var(--ls-ibad); box-shadow: 0 0 6px var(--ls-ibad); }
  .srv-pill.offline { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
  .srv-pill.offline .srv-dot { background: var(--accent); }
  @media (max-width: 560px) { .srv-pill { display: none; } }
  .controls { display: flex; align-items: center; gap: var(--space-2); }
  .time-rail { max-width: var(--content-max); margin: 10px auto 0; padding: 8px var(--space-4) 0; border-top: 1px solid var(--edge); }

  /* Understated V1 escape hatch. Menu-level, amber on hover, never a banner. */
  .to-v1 {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase; text-decoration: none;
    color: var(--text-muted); border: 1px solid transparent; border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); white-space: nowrap;
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .to-v1:hover { color: var(--accent-text); border-color: var(--border); }

  /* Account chip: sign-in entry point + linked identity readout. */
  .account { display: inline-flex; align-items: center; gap: var(--space-2); }
  /* Mailbox bell, settings gear and the help question mark share their chrome
     (amber on hover); on the bell the unread badge is the only lit element. */
  .bell, .gear, .help {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 2rem; height: 2rem; text-decoration: none;
    color: var(--text-muted); border: 1px solid var(--border); border-radius: var(--radius-sm);
    background: var(--bg-elevated); transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .bell:hover, .gear:hover, .help:hover { border-color: var(--accent); color: var(--text); }
  .bell-glyph, .gear-glyph, .help-glyph { font-size: var(--text-base); line-height: 1; }
  .help { margin-left: var(--space-1); }
  .bell-badge {
    position: absolute; top: -6px; right: -6px;
    min-width: 1.05rem; height: 1.05rem; padding: 0 3px;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; line-height: 1; color: var(--bg-deep);
    background: var(--accent); border-radius: 999px;
    box-shadow: 0 0 8px var(--accent-glow);
  }
  .acct-id {
    display: inline-flex; flex-direction: column; line-height: 1.15;
    max-width: 9rem; text-align: right;
  }
  .acct-char {
    font-size: var(--text-sm); color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .acct-handle {
    font-size: var(--text-xs); color: var(--text-muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .acct-out {
    background: var(--bg-elevated); color: var(--text-muted);
    border: 1px solid var(--border); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-3); font-size: var(--text-sm); cursor: pointer;
  }
  .acct-out:hover { border-color: var(--accent); color: var(--text); }
  .acct-login {
    text-decoration: none; color: var(--bg-deep);
    background: var(--accent); border: 1px solid var(--accent);
    border-radius: var(--radius-sm); font-family: var(--font-display);
    letter-spacing: .06em; font-size: var(--text-sm);
    padding: var(--space-1) var(--space-4);
    box-shadow: 0 0 10px var(--accent-glow); transition: filter var(--motion-fast) var(--ease-out);
  }
  .acct-login:hover { filter: brightness(1.12); }
  .acct-loading {
    width: 60px; height: 22px; border-radius: var(--radius-sm);
    background: var(--bg-elevated); opacity: .5;
  }
  @media (max-width: 520px) {
    .acct-id { max-width: 6.5rem; }
    .acct-char { display: none; }
  }
  /* Narrow phones: brand + controls no longer fit one row (overflowed ~29px at
     375px). Wrap the controls onto their own right-aligned row and compact the
     control chrome; the row heights stay honest for pages that measure the
     topbar (maps board). */
  @media (max-width: 480px) {
    .topbar-inner { flex-wrap: wrap; row-gap: var(--space-2); }
    .brand { flex: 1 0 auto; }
    /* Two rows, not three: the controls dissolve into the bar so Classic sits on
       the brand row and only the account cluster takes a row of its own. */
    .controls { display: contents; }
    .account { flex: 1 1 100%; min-width: 0; justify-content: flex-end; gap: var(--space-1); }
    .acct-login { padding: var(--space-1) var(--space-3); font-size: var(--text-xs); }
  }

  /* Primary nav strip: a second header row, horizontally scrollable on narrow
     screens (no wrap, no visible scrollbar). */
  .mainnav { margin-top: var(--space-3); border-top: 1px solid var(--border-subtle); }
  .mainnav-inner {
    max-width: var(--content-max); margin: 0 auto; padding: var(--space-1) var(--space-4) 0;
    display: flex; gap: var(--space-2); overflow-x: auto; scrollbar-width: none;
  }
  .mainnav-inner::-webkit-scrollbar { display: none; }
  /* A group is a labelled cluster, not a menu: the label sits ABOVE its links so
     five groups still fit the bar at desktop width instead of scrolling a wide
     screen. Three groups keep a label that is pure chrome; the two with a hub
     route render the same words as a link (see `hub` in NAV). */
  .navgroup { flex: 0 0 auto; display: flex; flex-direction: column; }
  .navgroup + .navgroup { border-left: 1px solid var(--border-subtle); padding-left: var(--space-2); }
  .navgroup-label {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: .2em;
    text-transform: uppercase; color: var(--text-muted); opacity: .5;
    white-space: nowrap; padding: 0 var(--space-3) 2px;
  }
  /* Hub label: same chrome at rest, but it has to answer the pointer, and it
     carries the amber current-state the sub-links use. */
  a.navgroup-label {
    text-decoration: none;
    transition: color var(--motion-fast) var(--ease-out), opacity var(--motion-fast) var(--ease-out);
  }
  a.navgroup-label:hover { color: var(--text); opacity: 1; }
  a.navgroup-label.on { color: var(--accent-bright); opacity: 1; }
  .navgroup-links { display: flex; align-items: flex-end; }
  .navlink {
    flex: 0 0 auto; text-decoration: none; white-space: nowrap;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .18em; text-transform: uppercase;
    color: var(--text-muted); padding: var(--space-1) var(--space-3) var(--space-2);
    border-bottom: 2px solid transparent; transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .navlink:hover { color: var(--text); }
  .navlink.on { color: var(--accent-bright); border-bottom-color: var(--accent); }
  @media (max-width: 480px) {
    .navgroup-label { padding: 0 var(--space-2) 2px; }
    .navlink { padding: var(--space-1) var(--space-2) var(--space-2); letter-spacing: .12em; }
  }
  /* Mirrors .bell-badge, inline instead of pinned: a count of things that have
     actually happened, so it earns Ibad rather than the amber chrome. */
  .nav-badge {
    margin-left: 5px; min-width: 1.05rem; height: 1.05rem; padding: 0 3px;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; line-height: 1; color: var(--bg-deep);
    background: var(--ls-ibad); border-radius: 999px;
    vertical-align: middle;
  }
  /* Footer: deliberately quiet. Muted type, amber only on hover, never Ibad
     (reserved for live data). The shell reserves its height so a short page
     does not gain a scrollbar purely because the footer exists. */
  .shell { flex: 1 0 auto; }   /* body is a flex column; no header-height arithmetic */
  /* Phone: the bottom tab bar replaces the strip; the footer keeps clear of it. */
  @media (max-width: 759px) {
    .mainnav { display: none; }
    .ftr { padding-bottom: calc(60px + env(safe-area-inset-bottom)); }
  }
  .ftr { border-top: 1px solid var(--border-subtle); padding: var(--space-3) 0; }
  .ftr-inner {
    max-width: var(--content-max); margin: 0 auto; padding: 0 var(--space-4);
    display: flex; align-items: center; justify-content: space-between;
    gap: var(--space-3); flex-wrap: wrap;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .12em;
    color: var(--text-muted);
  }
  .ftr-links { display: inline-flex; align-items: center; gap: var(--space-2); }
  .ftr a {
    color: var(--text-muted); text-decoration: none;
    transition: color var(--motion-fast) var(--ease-out);
  }
  .ftr a:hover { color: var(--accent); }
  .ftr-sep { opacity: .5; }
  .ftr-ver { margin-left: var(--space-2); opacity: .7; }
  .ftr-ver:hover { opacity: 1; }
  .topbar, .shell, .ftr { margin-left: 104px; min-width: 0; }
  .brand-text { font-size: 20px; letter-spacing: .08em; }
  .to-v1 { display: none; }
  .topbar-inner { gap: 20px; }
  .mainnav { border-top: 0; margin-top: 10px; }
  .navgroup { flex-direction: row; align-items: center; min-width: 0; }
  .navgroup-label { opacity: 1; font: 500 13px var(--font-sans); letter-spacing: 0; text-transform: none; padding: 8px 16px 8px 0; }
  .navgroup-links { align-items: center; }
  .navlink { font: 400 14px var(--font-sans); letter-spacing: 0; text-transform: none; padding: 8px 14px; }
  .bell, .gear, .help { width: 40px; height: 40px; }
  .acct-out { font-size: 12px; }
  @media (max-width: 1100px) { .brand-text, .srv-pill, .acct-handle { display: none; } }
  @media (max-width: 759px) {
    .topbar, .shell, .ftr { margin-left: 0; }
    .topbar-inner { gap: 8px; flex-wrap: nowrap; }
    .brand { display: none; }
    .controls, .account { display: flex; flex: 0 0 auto; width: auto; gap: 4px; }
    .acct-id, .acct-out, .gear { display: none; }
    .acct-login { font-size: 12px; padding: 10px 8px; white-space: nowrap; }
    .ftr { padding-bottom: calc(90px + env(safe-area-inset-bottom)); }
    .ftr-inner { font-size: 11px; letter-spacing: 0; }
    .skip-link { left: 12px; }
  }
</style>
