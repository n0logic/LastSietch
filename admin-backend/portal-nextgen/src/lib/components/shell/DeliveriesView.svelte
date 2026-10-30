<script>
  // Inbox > Deliveries (P3): the packages the server sent you, on a tab of their
  // own. The panel carries every seal of its own; this keeps the one login gate,
  // exactly as the Mailbox page does.
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import DeliveriesPanel from '$lib/components/mailbox/DeliveriesPanel.svelte';

  const gate = useAuthGate();
</script>

<svelte:head>
  <title>Deliveries | Inbox | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | courier"
    title="Deliveries"
    sub="Packages the server sent you, and where each part of them went."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see the packages the server sent you."
    />
  {:else}
    <DeliveriesPanel />
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
</style>
