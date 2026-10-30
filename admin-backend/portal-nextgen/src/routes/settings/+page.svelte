<script>
  // Settings: the one anchor page for everything about YOU rather than about the
  // desert, sorted into five categories on one route (`?tab=`, see
  // settingsTabs.js). Desktop shows the category list beside the open category;
  // a phone lands on the list and drills into one category with a back row.
  // Only the open category mounts, so its panels are the only ones that fetch.
  //
  // Every panel is its own component with its own load and its own sealed
  // states, so one failing read never takes the page with it. Signed-out seals
  // to an honest Connect-Discord panel; a signed-in empty state never offers a
  // second login.
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { SETTINGS_TABS, settingsTab } from '$lib/settingsTabs.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import IdentityCodePanel from '$lib/components/settings/IdentityCodePanel.svelte';
  import CharactersPanel from '$lib/components/settings/CharactersPanel.svelte';
  import AppearancePanel from '$lib/components/settings/AppearancePanel.svelte';
  import LayoutPanel from '$lib/components/settings/LayoutPanel.svelte';
  import ExportPanel from '$lib/components/settings/ExportPanel.svelte';
  import VisibilityPanel from '$lib/components/settings/VisibilityPanel.svelte';
  import ActivityLog from '$lib/components/settings/ActivityLog.svelte';
  import TransferHistory from '$lib/components/settings/TransferHistory.svelte';
  import SecurityPanel from '$lib/components/settings/SecurityPanel.svelte';
  import WeekendRemind from '$lib/components/events/WeekendRemind.svelte';

  const gate = useAuthGate();

  const asked = $derived($page.url.searchParams.get('tab'));
  const tab = $derived(settingsTab(asked, $page.url.hash));
  const current = $derived(SETTINGS_TABS.find((t) => t.id === tab));
  // A phone opens on the list unless the link named a category (or an old anchor).
  const drilled = $derived(asked !== null || $page.url.hash !== '');
</script>

<svelte:head>
  <title>{current.label} | Settings | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Stillsuit fitting"
    title="Settings"
    sub="Sign-in, characters, display, privacy and your transfer history, in five sections."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to manage your sign-in methods, linked accounts, appearance, identity code and activity."
    />
  {:else}
    <div class="settings" class:drilled>
      <nav class="cats" aria-label="Settings sections">
        <ul>
          {#each SETTINGS_TABS as t, i (t.id)}
            <li>
              <a
                class="cat"
                href={`${base}/settings?tab=${t.id}`}
                aria-current={t.id === tab ? 'page' : undefined}
              >
                <span class="cat-num mono" aria-hidden="true">0{i + 1}</span>
                <span class="cat-text">
                  <span class="cat-label">{t.label}</span>
                  <span class="cat-sum">{t.summary}</span>
                </span>
                <span class="cat-go" aria-hidden="true">›</span>
              </a>
            </li>
          {/each}
        </ul>
      </nav>

      <section class="pane" aria-labelledby="settings-pane-title">
        <a class="back mono" href={`${base}/settings`}><span aria-hidden="true">‹</span> Settings</a>
        <h2 id="settings-pane-title" class="pane-title">{current.label}</h2>

        {#if tab === 'account'}
          <SecurityPanel />
          <WeekendRemind />
        {:else if tab === 'characters'}
          <CharactersPanel />
        {:else if tab === 'display'}
          <div class="pair">
            <AppearancePanel />
            <LayoutPanel />
          </div>
        {:else if tab === 'privacy'}
          <VisibilityPanel />
          <ExportPanel />
        {:else if tab === 'transfers'}
          <IdentityCodePanel />
          <ActivityLog />
          <TransferHistory />
        {/if}
      </section>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .settings {
    margin-top: var(--space-4); display: grid; gap: var(--space-5);
    grid-template-columns: 17rem minmax(0, 1fr); align-items: start;
  }

  .cats { position: sticky; top: var(--space-4); }
  .cats ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .cat {
    position: relative; display: flex; align-items: flex-start; gap: var(--space-3);
    padding: var(--space-3) var(--space-4); min-height: 56px; box-sizing: border-box;
    color: var(--text); text-decoration: none;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    transition: border-color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .cat:hover { border-color: var(--edge-hi); background: var(--metal-1); }
  .cat[aria-current='page'] {
    border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, var(--metal-1));
    box-shadow: inset 3px 0 0 var(--accent);
  }
  .cat-num { font-size: var(--text-xs); letter-spacing: .12em; color: var(--text-muted); padding-top: 3px; }
  .cat[aria-current='page'] .cat-num { color: var(--accent-text); }
  .cat-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
  .cat-label { font-size: var(--text-base); line-height: 1.3; }
  .cat[aria-current='page'] .cat-label { color: var(--accent-bright); }
  .cat-sum { font-size: var(--text-xs); color: var(--text-muted); line-height: 1.4; }
  .cat-go { display: none; color: var(--text-muted); font-size: var(--text-lg); align-self: center; }

  .pane { min-width: 0; display: flex; flex-direction: column; gap: var(--space-4); }
  .pane-title {
    margin: 0; font-size: var(--text-lg); letter-spacing: .04em;
    padding-bottom: var(--space-3); border-bottom: 1px solid var(--border-subtle);
  }
  .back { display: none; }

  .pair { display: grid; gap: var(--space-4); grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: start; }

  @media (max-width: 1100px) {
    .pair { grid-template-columns: 1fr; }
  }

  /* Phone: the list is the landing; a named category replaces it, with a back row. */
  @media (max-width: 760px) {
    .settings { grid-template-columns: 1fr; gap: var(--space-4); }
    .cats { position: static; }
    .cat { align-items: center; padding: var(--space-4); }
    .cat-go { display: block; }
    .settings:not(.drilled) .pane { display: none; }
    .settings.drilled .cats { display: none; }
    .settings:not(.drilled) .cat[aria-current='page'] { border-color: var(--edge); background: var(--metal-0); box-shadow: none; }
    .settings:not(.drilled) .cat[aria-current='page'] .cat-label { color: var(--text); }
    .settings:not(.drilled) .cat[aria-current='page'] .cat-num { color: var(--text-muted); }
    .back {
      display: inline-flex; align-items: center; gap: var(--space-2); align-self: flex-start;
      min-height: 44px; padding: 0 var(--space-2); margin-left: calc(-1 * var(--space-2));
      font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
      color: var(--accent-text); text-decoration: none;
    }
  }
</style>
