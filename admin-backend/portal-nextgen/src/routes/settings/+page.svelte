<script>
  // Settings: the one anchor page for everything about YOU rather than about the
  // desert. It absorbs the controls the topbar and two other routes were
  // carrying: linked accounts and the alt link, the account and character
  // switchers, day/night and house colours, the personal export (still
  // flag-gated), directory visibility (off the Mailbox), the player's own
  // activity, and the identity code with its QR.
  //
  // Every panel is its own component with its own load and its own sealed
  // states, so one failing read never takes the page with it. Signed-out seals
  // to an honest Connect-Discord panel; a signed-in empty state never offers a
  // second login.
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import IdentityCodePanel from '$lib/components/settings/IdentityCodePanel.svelte';
  import LinkedAccountsPanel from '$lib/components/settings/LinkedAccountsPanel.svelte';
  import SwitchersPanel from '$lib/components/settings/SwitchersPanel.svelte';
  import AppearancePanel from '$lib/components/settings/AppearancePanel.svelte';
  import LayoutPanel from '$lib/components/settings/LayoutPanel.svelte';
  import ExportPanel from '$lib/components/settings/ExportPanel.svelte';
  import VisibilityPanel from '$lib/components/settings/VisibilityPanel.svelte';
  import ActivityLog from '$lib/components/settings/ActivityLog.svelte';
  import TransferHistory from '$lib/components/settings/TransferHistory.svelte';
  import SecurityPanel from '$lib/components/settings/SecurityPanel.svelte';

  const gate = useAuthGate();
</script>

<svelte:head>
  <title>Settings | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Stillsuit fitting"
    title="Settings"
    sub="Your profile, linked game accounts, selected character, appearance, identity code and activity."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to manage your linked accounts, appearance, identity code and activity."
    />
  {:else}
    <div class="stack">
      <SecurityPanel />
      <IdentityCodePanel />

      <div class="pair">
        <LinkedAccountsPanel />
        <SwitchersPanel />
      </div>

      <div class="pair">
        <AppearancePanel />
        <LayoutPanel />
      </div>

      <ExportPanel />

      <VisibilityPanel />
      <ActivityLog />
      <TransferHistory />
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .stack { margin-top: var(--space-4); display: flex; flex-direction: column; gap: var(--space-4); }
  .pair { display: grid; gap: var(--space-4); grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: start; }

  @media (max-width: 860px) {
    .pair { grid-template-columns: 1fr; }
  }
</style>
