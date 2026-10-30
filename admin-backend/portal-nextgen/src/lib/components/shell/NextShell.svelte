<script>
  // The P3 shell, drawn only when the shell switch is on (shell.next). The shipped
  // header, strip and rail in +layout.svelte are the other branch and stay as they
  // were.
  //
  // Same layout as the admin shell (owner 2026-10-30): a left rail with the five
  // destinations, a topbar with Search, Chat, Inbox and Help, the section's pages
  // as a tab strip over the content, and a bottom tab bar on phones. No dropdowns,
  // no fly-outs: a rail item is a link to its section's landing tab. Which tab is
  // lit comes from the URL alone (navNext.js), so deep links and back/forward work.
  //
  // Every count and status the layout already reads is handed in; this component
  // makes no reads of its own besides what the views it hosts make.
  import { base } from '$app/paths';
  import { version } from '$app/environment';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { auth, logout, LOGIN_URL } from '$lib/auth.svelte.js';
  import { landingHref, locate, railSections, redirectFor, renderFor, searchSections, visibleTabs, visibleViews } from '$lib/navNext.js';
  import BrandMark from '$lib/components/BrandMark.svelte';
  import Reach from '$lib/reach/Reach.svelte';
  import OrdersView from './OrdersView.svelte';
  import DeliveriesView from './DeliveriesView.svelte';
  import DesertNow from './DesertNow.svelte';
  // Retired pages that live on as a view of another page: the same component,
  // drawn under its new tab.
  import StandingsPage from '../../../routes/standings/+page.svelte';
  import SolidoPage from '../../../routes/solido/+page.svelte';
  import ActivityPage from '../../../routes/activity/+page.svelte';

  let {
    features = {},
    serverStatus = 'unknown',
    firedAlerts = 0,
    showChat = false,
    chatUnread = 0,
    mailUnread = 0,
    activeChar = '',
    children,
  } = $props();

  let rel = $derived(base && $page.url.pathname.startsWith(base) ? $page.url.pathname.slice(base.length) || '/' : $page.url.pathname);
  let search = $derived($page.url.search);
  let now = $state(Date.now());
  let loc = $derived(locate(rel, search, features, now));
  let render = $derived(renderFor(loc));
  let tabs = $derived(loc.section ? visibleTabs(loc.section, features, now) : []);
  let views = $derived(loc.tab ? visibleViews(loc.tab, features, now) : []);
  let rail = railSections();
  let reachSections = $derived(searchSections(features, now).map((s) => ({ ...s, items: s.items.map((it) => ({ ...it, href: `${base}${it.href}` })) })));

  // Retired URLs move to their new tab, query kept. replaceState so Back leaves
  // the portal the way it would have from the old page.
  $effect(() => {
    const t = Date.now();
    now = t;
    const to = redirectFor(rel, search, t);
    if (to) goto(`${base}${to}`, { replaceState: true });
  });

  // On a phone the strip scrolls sideways; keep the lit tab in view after a move.
  let tabsEl = $state(null);
  $effect(() => {
    const id = loc.tab?.id;
    if (!tabsEl || !id) return;
    const on = tabsEl.querySelector('.ns-tab.on');
    if (!on) return;
    const left = on.offsetLeft - tabsEl.offsetLeft;
    if (left < tabsEl.scrollLeft || left + on.offsetWidth > tabsEl.scrollLeft + tabsEl.clientWidth) {
      tabsEl.scrollLeft = Math.max(0, left - 24);
    }
  });

  function href(path) { return `${base}${path}`; }
  let chatOn = $derived(loc.section?.id === 'chat');
  let inboxOn = $derived(loc.section?.id === 'inbox');
  let helpOn = $derived(loc.section?.id === 'help');
  let settingsOn = $derived(loc.tab?.id === 'settings');
  let initials = $derived((activeChar || auth.discordHandle || '?').trim().slice(0, 2).toUpperCase());

  const ICONS = {
    home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20 L12 5 L20 20 Z"/><path d="M9.5 20 L12 14 L14.5 20"/></svg>',
    desert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 17c3-5 6-7 9-5s5 4 11-3"/><path d="M2 20h20"/></svg>',
    trade: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 7v10M9.5 9.5h4a1.5 1.5 0 0 1 0 3h-3a1.5 1.5 0 0 0 0 3h4"/></svg>',
    sietch: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="10" r="2.5"/><path d="M3 20c0-3.5 2.5-6 6-6s6 2.5 6 6M15 20c0-2.5 1-4 3.5-4s3.5 1.5 3.5 4"/></svg>',
    you: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-4 3-6.5 7-6.5s7 2.5 7 6.5"/></svg>',
  };
</script>

<div class="ns">
  <aside class="ns-rail" aria-label="Last Sietch">
    <a class="ns-brand" href={href('/')}>
      <BrandMark size={34} />
      <span class="ns-brand-t">Last Sietch<small>Companion</small></span>
    </a>
    <nav class="ns-rail-nav" aria-label="Portal sections">
      {#each rail as s (s.id)}
        <a
          class="ns-rail-i"
          class:on={loc.section?.id === s.id}
          href={href(landingHref(s))}
          aria-current={loc.section?.id === s.id ? 'page' : undefined}
        >{@html ICONS[s.id]}<span>{s.label}</span>{#if s.badge === 'alerts' && firedAlerts > 0}<span class="ns-count mono" aria-label="{firedAlerts} price alerts fired">{firedAlerts > 99 ? '99+' : firedAlerts}</span>{/if}</a>
      {/each}
    </nav>
    <div class="ns-rail-foot">
      {#if auth.status === 'authed'}
        <span class="ns-who">
          {#if activeChar}<span class="ns-who-char">{activeChar}</span>{/if}
          <span class="ns-who-handle mono">{auth.discordHandle || 'Linked'}</span>
        </span>
        <button class="ns-out" type="button" onclick={logout}>Sign out</button>
      {:else if auth.status === 'anon'}
        <a class="ns-login" href={LOGIN_URL} data-sveltekit-reload>Sign in</a>
      {/if}
    </div>
  </aside>

  <div class="ns-main">
    <header class="ns-top">
      <a class="ns-top-brand" href={href('/')} aria-label="Last Sietch home"><BrandMark size={28} /></a>
      <Reach sections={reachSections} />
      <div class="ns-utils">
        {#if serverStatus !== 'unknown'}
          <span class="ns-srv {serverStatus}" title={serverStatus === 'online' ? 'Server online' : 'Server offline or in maintenance'}>
            <span class="ns-srv-dot" aria-hidden="true"></span>{serverStatus === 'online' ? 'Online' : 'Offline'}
          </span>
        {/if}
        {#if showChat}
          <a class="ns-u" class:on={chatOn} href={href('/chat')} aria-current={chatOn ? 'page' : undefined} aria-label={chatUnread > 0 ? `Chat, ${chatUnread} unread` : 'Chat'} title="Chat">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 5.5h15v10h-9l-4.5 3.5v-3.5h-1.5z" /></svg>
            {#if chatUnread > 0}<span class="ns-badge mono">{chatUnread > 99 ? '99+' : chatUnread}</span>{/if}
          </a>
        {/if}
        {#if auth.status === 'authed'}
          <a class="ns-u" class:on={inboxOn} href={href('/mailbox')} aria-current={inboxOn ? 'page' : undefined} aria-label={mailUnread > 0 ? `Inbox, ${mailUnread} unread` : 'Inbox'} title="Inbox">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 6.5h17v11h-17z"/><path d="m3.5 7 8.5 6.5L20.5 7"/></svg>
            {#if mailUnread > 0}<span class="ns-badge mono">{mailUnread > 99 ? '99+' : mailUnread}</span>{/if}
          </a>
        {/if}
        <a class="ns-u" class:on={helpOn} href={href('/help')} aria-current={helpOn ? 'page' : undefined} aria-label="Help" title="Help and what's new">
          <span class="ns-q mono" aria-hidden="true">?</span>
        </a>
        {#if auth.status === 'authed'}
          <a class="ns-you" class:on={settingsOn} href={href('/settings')} aria-current={settingsOn ? 'page' : undefined} aria-label="You: settings" title="Settings">
            <span class="ns-you-tile mono" aria-hidden="true">{initials}</span>
          </a>
        {:else if auth.status === 'anon'}
          <a class="ns-login ns-login-top" href={LOGIN_URL} data-sveltekit-reload>Sign in</a>
        {/if}
      </div>
    </header>

    {#if loc.section && (tabs.length > 1 || views.length > 1)}
      <nav class="ns-tabs" aria-label={`${loc.section.label} pages`} bind:this={tabsEl}>
        <span class="ns-tabs-label mono" aria-hidden="true">{loc.section.label}</span>
        {#if tabs.length > 1}
          {#each tabs as t (t.id)}
            <a
              class="ns-tab"
              class:on={loc.tab?.id === t.id}
              href={href(t.href)}
              aria-current={loc.tab?.id === t.id ? 'page' : undefined}
            >{t.label}{#if t.badge === 'alerts' && firedAlerts > 0}<span class="ns-count mono" aria-label="{firedAlerts} price alerts fired">{firedAlerts > 99 ? '99+' : firedAlerts}</span>{/if}</a>
          {/each}
        {/if}
      </nav>
      {#if views.length > 1}
        <nav class="ns-views" aria-label={`${loc.tab.label} views`}>
          {#each views as v (v.id)}
            <a
              class="ns-view"
              class:on={loc.view?.id === v.id}
              href={href(v.href)}
              aria-current={loc.view?.id === v.id ? 'page' : undefined}
            >{v.label}</a>
          {/each}
        </nav>
      {/if}
    {/if}

    <main class="ns-content" id="main-content" tabindex="-1">
      {#if render === 'orders'}
        <OrdersView />
      {:else if render === 'standings'}
        <StandingsPage />
      {:else if render === 'preview'}
        <SolidoPage />
      {:else if render === 'deliveries'}
        <DeliveriesView />
      {:else if render === 'activity'}
        <ActivityPage />
      {:else}
        {@render children?.()}
      {/if}
      {#if loc.tab?.id === 'maps' && (rel === '/maps' || rel === '/maps/')}
        <DesertNow />
      {/if}
    </main>

    <footer class="ns-ftr" role="contentinfo">
      <span>Last Sietch · Dune: Awakening community server
        <a class="ns-ver" href={href('/help')} title="What's new in this release">v{version}</a></span>
      <span class="ns-ftr-links">
        <a href={href('/rules')}>Rules</a>
        <span aria-hidden="true">·</span>
        <a href={href('/help')}>Help</a>
        <span aria-hidden="true">·</span>
        <a href="/portal/account" data-sveltekit-reload>Classic</a>
        <span aria-hidden="true">·</span>
        <a href="https://ko-fi.com/lastsietch" rel="noopener" target="_blank">Tip Jar</a>
        <span aria-hidden="true">·</span>
        <a href="https://discord.gg/your-invite" rel="noopener" target="_blank">Discord</a>
        <span aria-hidden="true">·</span>
        <a href="https://www.facebook.com/profile.php?id=61594528787402" rel="noopener" target="_blank">Facebook</a>
      </span>
    </footer>
  </div>

  <!-- Phone: the same five destinations as a bottom bar. Same rule as the rail:
       each item is a link to its landing tab, never a sheet of children. -->
  <nav class="ns-tabbar" aria-label="Portal sections">
    {#each rail as s (s.id)}
      <a
        class="ns-tb"
        class:on={loc.section?.id === s.id}
        href={href(landingHref(s))}
        aria-current={loc.section?.id === s.id ? 'page' : undefined}
      >{@html ICONS[s.id]}<span>{s.label}</span>{#if s.badge === 'alerts' && firedAlerts > 0}<span class="ns-dot" aria-hidden="true"></span>{/if}</a>
    {/each}
  </nav>
</div>

<style>
  /* Admin shell patterns (static/css/v2/shell.css) in the portal's own tokens:
     rail item = 3px amber edge + amber wash when current; tabs = uppercase display
     type with a diamond bullet and a 3px underline. Ibad stays on live markers. */
  .ns { display: grid; grid-template-columns: 216px minmax(0, 1fr); min-height: 100dvh; }
  .ns-main { min-width: 0; display: flex; flex-direction: column; }
  .ns-content { flex: 1 0 auto; min-width: 0; }

  .ns-rail {
    position: sticky; top: 0; height: 100dvh; align-self: start; z-index: 40;
    display: flex; flex-direction: column; gap: 18px; padding: 16px 12px;
    background: var(--rail-surface, var(--bg-deep)); border-right: 1px solid var(--edge-hi);
  }
  .ns-brand { display: flex; align-items: center; gap: 10px; padding: 4px; text-decoration: none; color: var(--text); }
  .ns-brand-t {
    display: flex; flex-direction: column; font: 700 1.05rem/1.1 var(--font-display);
    letter-spacing: .08em; text-transform: uppercase;
  }
  .ns-brand-t small { font: 500 .7rem var(--font-mono); color: var(--text-muted); letter-spacing: .16em; }
  .ns-rail-nav { display: grid; gap: 2px; }
  .ns-rail-i {
    display: flex; align-items: center; gap: 12px; min-height: 44px; padding: 8px 12px;
    border-left: 3px solid transparent; border-radius: 4px; text-decoration: none;
    font: 600 1rem var(--font-display); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text-muted);
    transition: color var(--motion-fast) var(--ease-in-out), background var(--motion-fast) var(--ease-in-out);
  }
  .ns-rail-i :global(svg), .ns-tb :global(svg), .ns-u svg {
    width: 20px; height: 20px; flex: none; fill: none; stroke: currentColor;
    stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round;
  }
  .ns-rail-i:hover { color: var(--text); background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .ns-rail-i.on { color: var(--accent-bright); border-left-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, transparent); }
  .ns-rail-i .ns-count { margin-left: auto; }
  .ns-rail-foot { margin-top: auto; display: grid; gap: 8px; padding: 0 4px; }
  .ns-who { display: flex; flex-direction: column; min-width: 0; line-height: 1.2; }
  .ns-who-char { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ns-who-handle { font-size: var(--text-xs); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ns-out {
    justify-self: start; min-height: 36px; padding: 0 12px; cursor: pointer;
    color: var(--text-muted); background: transparent; border: 1px solid var(--edge); border-radius: var(--radius-sm); font: inherit; font-size: 12px;
  }
  .ns-out:hover { border-color: var(--accent); color: var(--text); }
  .ns-login {
    justify-self: start; display: inline-flex; align-items: center; min-height: 40px; padding: 0 16px;
    text-decoration: none; color: var(--bg-deep); background: var(--accent); border-radius: var(--radius-sm);
    font: 600 var(--text-sm) var(--font-display); letter-spacing: .08em; text-transform: uppercase;
  }
  .ns-login:hover { filter: brightness(1.12); }

  .ns-top {
    position: sticky; top: 0; z-index: 30;
    display: flex; align-items: center; gap: 14px;
    padding: calc(10px + env(safe-area-inset-top)) 24px 10px;
    background: color-mix(in srgb, var(--bg-deep) 90%, transparent); backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--edge);
  }
  .ns-top-brand { display: none; }
  .ns-utils { display: flex; align-items: center; gap: 8px; margin-left: auto; }
  .ns-srv {
    display: inline-flex; align-items: center; gap: 6px; margin-right: 4px; padding: 2px 10px;
    border: 1px solid var(--edge); border-radius: 999px; white-space: nowrap;
    font: 500 var(--text-xs) var(--font-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--text-muted);
  }
  .ns-srv-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); }
  .ns-srv.online { color: var(--ls-ibad); border-color: color-mix(in srgb, var(--ls-ibad) 45%, transparent); }
  .ns-srv.online .ns-srv-dot { background: var(--ls-ibad); }
  .ns-srv.offline { color: var(--accent); }
  .ns-srv.offline .ns-srv-dot { background: var(--accent); }
  .ns-u, .ns-you {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 44px; height: 44px; text-decoration: none; color: var(--text-muted);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--bg-elevated);
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .ns-u:hover, .ns-you:hover { border-color: var(--accent); color: var(--text); }
  .ns-u.on, .ns-you.on { border-color: var(--accent); color: var(--accent-bright); }
  .ns-q { font-size: var(--text-base); line-height: 1; }
  .ns-you-tile { font-size: 13px; letter-spacing: .04em; }
  .ns-badge {
    position: absolute; top: -6px; right: -6px; min-width: 1.05rem; height: 1.05rem; padding: 0 3px;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; line-height: 1; color: var(--bg-deep); background: var(--accent); border-radius: 999px;
  }
  .ns-count {
    min-width: 1.05rem; height: 1.05rem; padding: 0 3px; margin-left: 6px;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; line-height: 1; letter-spacing: 0; color: var(--bg-deep); background: var(--ls-ibad); border-radius: 999px;
  }
  .ns-login-top { display: none; }

  .ns-tabs {
    display: flex; align-items: stretch; gap: 4px; padding: 0 24px; overflow-x: auto; scrollbar-width: none;
    background: var(--bg-elevated); border-bottom: 1px solid var(--edge);
  }
  .ns-tabs::-webkit-scrollbar { display: none; }
  .ns-tabs-label {
    display: inline-flex; align-items: center; padding-right: 12px; margin-right: 4px;
    border-right: 1px solid var(--edge); white-space: nowrap;
    font-size: 11px; letter-spacing: .22em; text-transform: uppercase; color: var(--text-muted);
  }
  .ns-tab {
    display: inline-flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 16px;
    border-bottom: 3px solid transparent; white-space: nowrap; text-decoration: none;
    font: 600 .9rem var(--font-display); letter-spacing: .12em; text-transform: uppercase; color: var(--text-muted);
    transition: color var(--motion-fast) var(--ease-in-out), border-color var(--motion-fast) var(--ease-in-out);
  }
  .ns-tab::before {
    content: ''; width: 7px; height: 7px; background: currentColor; opacity: .45;
    clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%);
  }
  .ns-tab:hover { color: var(--text); border-bottom-color: var(--edge-hi); }
  .ns-tab.on { color: var(--accent-bright); border-bottom-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
  .ns-tab.on::before { opacity: 1; }
  .ns-views { display: flex; gap: 6px; padding: 10px 24px 0; overflow-x: auto; scrollbar-width: none; }
  .ns-views::-webkit-scrollbar { display: none; }
  .ns-view {
    display: inline-flex; align-items: center; min-height: 36px; padding: 0 14px; white-space: nowrap;
    text-decoration: none; color: var(--text-muted); border: 1px solid var(--edge); border-radius: 999px;
    font: 500 var(--text-sm) var(--font-sans);
  }
  .ns-view:hover { color: var(--text); border-color: var(--edge-hi); }
  .ns-view.on { color: var(--accent-bright); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); }

  .ns-ftr {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
    padding: 14px 24px; border-top: 1px solid var(--border-subtle);
    font: 400 var(--text-xs) var(--font-mono); letter-spacing: .1em; color: var(--text-muted);
  }
  .ns-ftr a { color: var(--text-muted); text-decoration: none; }
  .ns-ftr a:hover { color: var(--accent); }
  .ns-ftr-links { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .ns-ver { margin-left: 8px; opacity: .7; }

  .ns-tabbar { display: none; }

  @media (max-width: 1100px) { .ns-srv { display: none; } }
  @media (max-width: 759px) {
    .ns { grid-template-columns: minmax(0, 1fr); }
    .ns-rail { display: none; }
    .ns-top { padding: calc(8px + env(safe-area-inset-top)) 12px 8px; gap: 8px; }
    .ns-top-brand { display: inline-flex; flex: none; }
    .ns-utils { gap: 4px; }
    .ns-login-top { display: inline-flex; min-height: 44px; padding: 0 10px; font-size: 12px; }
    .ns-tabs { padding: 0 8px; }
    .ns-tabs-label { display: none; }
    .ns-tab { padding: 0 12px; }
    .ns-views { padding: 10px 12px 0; }
    .ns-ftr { padding: 14px 12px calc(84px + env(safe-area-inset-bottom)); font-size: 11px; letter-spacing: 0; }
    .ns-tabbar {
      position: fixed; left: 0; right: 0; bottom: 0; z-index: 80;
      display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
      padding: 0 4px env(safe-area-inset-bottom);
      background: var(--rail-surface, var(--bg-elevated)); border-top: 1px solid var(--edge-hi);
    }
    .ns-tb {
      position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px;
      min-height: 64px; padding: 8px 0; min-width: 0; text-decoration: none; color: var(--text-muted);
      font: 500 11px var(--font-sans);
    }
    .ns-tb span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ns-tb.on { color: var(--accent-bright); }
    .ns-tb.on::before { content: ''; position: absolute; left: 22%; right: 22%; bottom: 0; height: 3px; background: var(--accent); }
    .ns-dot { position: absolute; top: 8px; right: calc(50% - 16px); width: 7px; height: 7px; border-radius: 50%; background: var(--ls-ibad); }
  }
</style>
