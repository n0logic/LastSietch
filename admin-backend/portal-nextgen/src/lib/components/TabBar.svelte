<script>
  // Phone navigation (owner ruling 2026-09-03): the v3 mockup's icon rail as a
  // fixed bottom tab bar. One item per nav group plus the settings gear; a group
  // with several routes opens a small sheet above the bar (Holdings has four),
  // a single-route group with no hub links straight through. Every route keeps its href and
  // reload attribute exactly as the grouped top bar renders them; this is a
  // second way in, not a second route table. Hidden at desktop width, where the
  // grouped strip stays.
  let {
    sections = [],
    isActive = () => false,
    settingsHref = '/settings',
    settingsActive = false,
    firedAlerts = 0,
  } = $props();

  let open = $state(null);      // group name whose sheet is showing
  let sheetEl = $state(null);
  let opener = null;            // the button that opened the sheet, for focus return

  // Sheet rows for a group. A hub group leads with its own page, labelled by the
  // group name: the strip's clickable label has no equivalent down here, so the
  // sheet is the only way a phone reaches /desert or /economy.
  function rowsFor(section) {
    return section.hub
      ? [{ label: section.group, href: section.hub, v2: true }, ...section.items]
      : section.items;
  }
  function groupActive(section) { return rowsFor(section).some((it) => isActive(it)); }
  function groupHasAlerts(section) { return section.items.some((it) => it.badge === 'alerts'); }
  function toggle(section, e) {
    if (open === section.group) { close(); return; }
    opener = e.currentTarget;
    open = section.group;
  }
  function close() {
    const back = opener;
    open = null; opener = null;
    back?.focus?.();
  }
  function onKey(e) {
    if (e.key === 'Escape' && open) { e.stopPropagation(); close(); }
  }
  $effect(() => {
    if (open && sheetEl) sheetEl.querySelector('a')?.focus();
  });
  let current = $derived(sections.find((s) => s.group === open));

  // Line icons from the concept mockup's rail (stroke, currentColor).
  const ICONS = {
    Home: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20 L12 5 L20 20 Z"/><path d="M9.5 20 L12 14 L14.5 20"/></svg>',
    Desert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 17c3-5 6-7 9-5s5 4 11-3"/><path d="M2 20h20"/></svg>',
    Economy: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 7v10M9.5 9.5h4a1.5 1.5 0 0 1 0 3h-3a1.5 1.5 0 0 0 0 3h4"/></svg>',
    Holdings: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 8l9-4 9 4v9l-9 4-9-4z"/><path d="M3 8l9 4 9-4M12 12v9"/></svg>',
    Sietch: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="10" r="2.5"/><path d="M3 20c0-3.5 2.5-6 6-6s6 2.5 6 6M15 20c0-2.5 1-4 3.5-4s3.5 1.5 3.5 4"/></svg>',
    gear: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/></svg>',
  };
</script>

<svelte:window onkeydown={onKey} />

{#if current}
  <div class="tb-scrim" onclick={close} aria-hidden="true"></div>
  <div class="tb-sheet" role="dialog" aria-label={`${current.group} sections`} bind:this={sheetEl}>
    <!-- A hub group's first row already says the group name, so the heading
         above it would print the same word twice in a row at phone width. -->
    {#if !current.hub}<p class="tb-sheet-label" aria-hidden="true">{current.group}</p>{/if}
    {#each rowsFor(current) as item}
      <a
        class="tb-link"
        class:on={isActive(item)}
        href={item.href}
        aria-current={isActive(item) ? 'page' : undefined}
        onclick={close}
        {...item.v2 ? {} : { 'data-sveltekit-reload': true }}
      >{item.label}{#if item.badge === 'alerts' && firedAlerts > 0}<span class="tb-badge mono" aria-label="{firedAlerts} price alerts fired">{firedAlerts > 99 ? '99+' : firedAlerts}</span>{/if}</a>
    {/each}
  </div>
{/if}

<nav class="tabbar" aria-label="Portal sections">
  {#each sections as section}
    {#if section.items.length === 1 && !section.hub}
      <a
        class="tb-item"
        class:on={groupActive(section)}
        href={section.items[0].href}
        aria-current={groupActive(section) ? 'page' : undefined}
        {...section.items[0].v2 ? {} : { 'data-sveltekit-reload': true }}
      >{@html ICONS[section.group]}<span>{section.group}</span></a>
    {:else}
      <button
        class="tb-item"
        class:on={groupActive(section)}
        type="button"
        aria-expanded={open === section.group}
        aria-current={groupActive(section) ? 'page' : undefined}
        onclick={(e) => toggle(section, e)}
      >{@html ICONS[section.group]}<span>{section.group}</span>{#if groupHasAlerts(section) && firedAlerts > 0}<span class="tb-dot" aria-hidden="true"></span>{/if}</button>
    {/if}
  {/each}
  <a class="tb-item" class:on={settingsActive} href={settingsHref} aria-label="Settings" aria-current={settingsActive ? 'page' : undefined}>{@html ICONS.gear}<span>You</span></a>
</nav>

<style>
  /* Below the desktop breakpoint the grouped strip hides (see +layout) and this
     bar shows; the two never render together. z-index sits under --z-dialog so
     every Modal still covers it. */
  .tabbar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 80;
    display: none; grid-template-columns: repeat(6, minmax(0, 1fr));
    background: var(--bg-elevated); border-top: 1px solid var(--border-subtle);
    padding-bottom: env(safe-area-inset-bottom);
  }
  @media (max-width: 759px) { .tabbar { display: grid; } }
  .tb-item {
    position: relative; display: flex; flex-direction: column; align-items: center; gap: 3px;
    padding: 8px 0 6px; min-width: 0; color: var(--text-muted); background: none; border: 0;
    text-decoration: none; cursor: pointer;
    font-family: var(--font-mono); font-size: 9px; letter-spacing: .12em; text-transform: uppercase;
    transition: color var(--motion-fast) var(--ease-out);
  }
  .tb-item :global(svg) { width: 22px; height: 22px; stroke: currentColor; fill: none; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
  .tb-item.on { color: var(--accent-bright); }
  .tb-item.on::before {
    content: ''; position: absolute; top: 0; left: 25%; right: 25%; height: 2px;
    background: var(--accent); box-shadow: 0 0 10px var(--accent-glow);
  }
  .tb-item span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* Live count marker: something happened, so it earns Ibad, like the nav badge. */
  .tb-dot { position: absolute; top: 6px; right: calc(50% - 16px); width: 7px; height: 7px; border-radius: 50%; background: var(--ls-ibad); }
  .tb-scrim { position: fixed; inset: 0; z-index: 79; background: rgba(0, 0, 0, .45); }
  .tb-sheet {
    position: fixed; left: 0; right: 0; bottom: calc(58px + env(safe-area-inset-bottom)); z-index: 80;
    display: grid; gap: var(--space-1);
    padding: var(--space-2) var(--space-3) var(--space-3);
    background: var(--bg-elevated); border-top: 1px solid var(--border-subtle);
    box-shadow: 0 -12px 30px rgba(0, 0, 0, .35);
  }
  .tb-sheet-label {
    margin: 0 0 var(--space-1); font-family: var(--font-mono); font-size: 10px; letter-spacing: .2em;
    text-transform: uppercase; color: var(--text-muted); opacity: .6;
  }
  .tb-link {
    display: flex; align-items: center; justify-content: space-between;
    padding: var(--space-2) var(--space-3); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
    text-decoration: none; color: var(--text);
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase;
  }
  .tb-link.on { color: var(--accent-bright); border-color: var(--accent); }
  .tb-badge {
    min-width: 1.05rem; height: 1.05rem; padding: 0 3px; display: inline-flex; align-items: center; justify-content: center;
    font-size: 10px; line-height: 1; color: var(--bg-deep); background: var(--ls-ibad); border-radius: 999px;
  }
  .tabbar { display: flex; flex-direction: column; top: 0; right: auto; width: 104px; border-top: 0; border-right: 1px solid var(--edge); padding: 24px 8px; gap: 8px; background: var(--rail-surface, var(--bg-elevated)); }
  .tb-item { justify-content: center; min-height: 72px; padding: 10px 2px; font: 500 12px var(--font-sans); letter-spacing: 0; text-transform: none; }
  .tb-item:last-child { margin-top: auto; }
  .tb-item.on::before { top: 16px; bottom: 16px; height: auto; width: 3px; left: -8px; right: auto; box-shadow: none; }
  .tb-sheet { left: 112px; top: 100px; right: auto; bottom: auto; width: 280px; max-height: calc(100dvh - 120px); overflow-y: auto; border: 1px solid var(--edge-hi); border-radius: 6px; padding: 12px; }
  .tb-link { min-height: 44px; font: 400 15px var(--font-sans); letter-spacing: 0; text-transform: none; }
  .tb-sheet-label { font: 500 13px var(--font-sans); opacity: 1; letter-spacing: 0; text-transform: none; padding: 6px; }
  @media (max-width: 759px) {
    .tabbar { display: grid; top: auto; width: auto; right: 0; grid-template-columns: repeat(6, minmax(0, 1fr)); padding: 0 4px env(safe-area-inset-bottom); border-right: 0; border-top: 1px solid var(--edge); gap: 0; }
    .tb-item { min-height: 64px; padding: 8px 0; font-size: 11px; }
    .tb-item:last-child { margin-top: 0; }
    .tb-item.on::before { top: auto; bottom: 0; left: 22%; right: 22%; height: 3px; width: auto; }
    .tb-sheet { left: 8px; right: 8px; top: auto; bottom: calc(72px + env(safe-area-inset-bottom)); width: auto; max-height: 65dvh; }
  }
</style>
