<script>
  import '../app.css';
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { version } from '$app/environment';
  import { page } from '$app/stores';
  import { auth, loadAuth, logout, LOGIN_URL } from '$lib/auth.svelte.js';
  import { prefs, loadPrefs } from '$lib/prefs.svelte.js';
  import { applyReadingFont, applyReadingSize } from '$lib/readingFont.js';
  import { api } from '$lib/api.js';
  import { mailbox, refreshUnread } from '$lib/mailbox.svelte.js';
  import { chatBadge, chatUnreadTotal, watchChatUnread } from '$lib/chatBadge.svelte.js';
  import { exchange, refreshAlerts } from '$lib/exchange.svelte.js';
  import { sendSolari, closeSendSolari } from '$lib/sendSolari.svelte.js';
  import BrandMark from '$lib/components/BrandMark.svelte';
  import ServerSchedule from '$lib/components/ServerSchedule.svelte';
  import SendSolariDialog from '$lib/components/SendSolariDialog.svelte';
  import TabBar from '$lib/components/TabBar.svelte';
  import Reach from '$lib/reach/Reach.svelte';
  import NextShell from '$lib/components/shell/NextShell.svelte';
  import { shell, loadShell, shellExpectedNext } from '$lib/shellNext.svelte.js';
  let { children } = $props();

  // P3: the redesigned shell (rail, topbar utilities, section tabs) draws only when
  // the shell switch is on. Until the switch is read, the new shell draws only when
  // the override or the last read says it is on, so a flag-off viewer gets the
  // shipped shell below at once and never sees the new one.
  const expectNext = shellExpectedNext();
  let nextShell = $derived(shell.status === 'ready' ? shell.next : expectNext);

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
      { label: 'Sand Table', href: `${base}/world`, v2: true },
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
    // Chat is not here: it is the button beside the mail bell, with its own
    // unread badge (owner ruling 2026-10-20). Same ruling set this order.
    { group: 'Sietch', items: [
      { label: 'Guilds', href: `${base}/guilds`, v2: true },
      { label: 'Events', href: `${base}/events`, v2: true },
      // Halloween 2026 (Seven Nights of the Sietch). Gated on the event flag, so
      // the entry leaves the bar the moment LASTSIETCH_SIETCH_NIGHTS_ENABLED is off.
      { label: 'Seven Nights', href: `${base}/sietch`, v2: true, feature: 'sietch', badge: 'live' },
      { label: 'Amtal', href: `${base}/amtal`, v2: true, feature: 'amtal' },
      { label: 'Reports', href: `${base}/reports`, v2: true, feature: 'reports' },
      { label: 'Landsraad', href: `${base}/landsraad`, v2: true },
      { label: 'Standings', href: `${base}/standings`, v2: true },
    ] },
  ];
  // Feature-gated nav items (item.feature) are hidden until /portal/features says
  // that gate is on. Fail closed: an unread features map hides every gated item.
  let navFeatures = $state({});
  let visibleNav = $derived(NAV.map((section) => ({ ...section, items: section.items.filter((item) => !item.feature || navFeatures[item.feature] === true) })));
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
    loadShell();
    api.server.features().then((data) => { navFeatures = (data && data.features) || {}; }).catch(() => { navFeatures = {}; });
    // Preferences read after the session is known: the mirror paints first,
    // the account doc replaces it once /portal/me has answered.
    loadAuth().then(() => loadPrefs()).then(() => {
      // A reading font (and its size) saved on the account follows the player to this browser;
      // with nothing saved there, the device choice stands.
      const rf = prefs.identity.reading_font;
      if (prefs.status === 'ready' && typeof rf === 'boolean') applyReadingFont(rf);
      const rsz = prefs.identity.reading_size;
      if (prefs.status === 'ready' && typeof rsz === 'string') applyReadingSize(rsz);
    });
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

  // Chat button beside the bell. Fails closed on the chat gate like the gated
  // nav items; the badge polls only while that button can be shown.
  let showChat = $derived(auth.status === 'authed' && navFeatures.chat === true && !chatBadge.dark);
  let chatUnread = $derived(showChat ? chatUnreadTotal() : 0);
  let chatActive = $derived(path.startsWith(`${base}/chat`));
  $effect(() => {
    if (auth.status === 'authed' && navFeatures.chat === true) return watchChatUnread();
  });
</script>

<a class="skip-link" href="#main-content">Skip to content</a>
{#if import.meta.env.VITE_REVIEW_PREVIEW === 'true'}
  <aside class="review-banner"><b>Local preview</b><span>Sample data. Actions stay local.</span><a href="/?preview=habbanya" data-sveltekit-reload>Habbanya</a><a href="/?preview=amtal" data-sveltekit-reload>Amtal</a><a href="/?preview=anon" data-sveltekit-reload>Signed out</a><a href="/start?preview=unlinked" data-sveltekit-reload>Unlinked</a><a href="http://localhost:4174/dune/">Joining page</a></aside>
{/if}
{#if nextShell}
<NextShell features={navFeatures} {serverStatus} {firedAlerts} {showChat} {chatUnread} mailUnread={mailbox.unread} {activeChar}>
  {@render children?.()}
</NextShell>
{/if}
{#if !nextShell}
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
          {#if showChat}
            <a class="chat-btn" class:on={chatActive} href={`${base}/chat`} aria-current={chatActive ? 'page' : undefined} aria-label={chatUnread > 0 ? `Chat, ${chatUnread} unread` : 'Chat'} title="Chat">
              <svg class="chat-glyph" viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 5.5h15v10h-9l-4.5 3.5v-3.5h-1.5z" /></svg>
              {#if chatUnread > 0}<span class="bell-badge mono">{chatUnread > 99 ? '99+' : chatUnread}</span>{/if}
            </a>
          {/if}
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
              >{item.label}{#if item.badge === 'alerts' && firedAlerts > 0}<span class="nav-badge mono" aria-label="{firedAlerts} price alerts fired">{firedAlerts > 99 ? '99+' : firedAlerts}</span>{/if}{#if item.badge === 'live'}<span class="nav-live">Live</span>{/if}</a>
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
      <a href="https://ko-fi.com/lastsietch" rel="noopener" target="_blank">Tip Jar</a>
      <span class="ftr-sep" aria-hidden="true">·</span>
      <a class="ftr-icon" href="https://discord.gg/your-invite" rel="noopener" target="_blank" aria-label="Last Sietch on Discord" title="Discord"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189Z"/></svg></a>
      <a class="ftr-icon" href="https://www.facebook.com/profile.php?id=61594528787402" rel="noopener" target="_blank" aria-label="Last Sietch on Facebook" title="Facebook"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.101 23.691v-7.98H6.627v-3.667h2.474v-1.58c0-4.085 1.848-5.978 5.858-5.978.401 0 .955.042 1.468.103a8.68 8.68 0 0 1 1.141.195v3.325a8.623 8.623 0 0 0-.653-.036 26.805 26.805 0 0 0-.733-.009c-.707 0-1.259.096-1.675.309a1.686 1.686 0 0 0-.679.622c-.258.42-.374.995-.374 1.752v1.297h3.919l-.386 2.103-.287 1.564h-3.246v8.245C19.396 23.238 24 18.179 24 12.044c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.628 3.874 10.35 9.101 11.647Z"/></svg></a>
    </span>
  </div>
</footer>
{/if}

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
  /* Chat, mailbox bell, settings gear and the help question mark share their
     chrome (amber on hover); on chat and the bell the unread badge is the only
     lit element. */
  .chat-btn, .bell, .gear, .help {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 2rem; height: 2rem; text-decoration: none;
    color: var(--text-muted); border: 1px solid var(--border); border-radius: var(--radius-sm);
    background: var(--bg-elevated); transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .chat-btn:hover, .bell:hover, .gear:hover, .help:hover { border-color: var(--accent); color: var(--text); }
  .chat-btn.on { border-color: var(--accent); color: var(--accent-bright); }
  .bell-glyph, .gear-glyph, .help-glyph { font-size: var(--text-base); line-height: 1; }
  .chat-glyph { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linejoin: round; }
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
  .nav-live {
    margin-left: 6px; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 700;
    letter-spacing: .06em; text-transform: uppercase; color: var(--bg-deep); background: var(--accent-text);
  }
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
  .ftr-icon { display: inline-flex; align-items: center; justify-content: center; min-width: 32px; min-height: 32px; }
  .ftr-icon svg { width: 16px; height: 16px; fill: currentColor; }
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
  .chat-btn, .bell, .gear, .help { width: 44px; height: 44px; }
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
