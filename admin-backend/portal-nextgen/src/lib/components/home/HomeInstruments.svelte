<script>
  // The instruments row plus the personal column. This component is the seam
  // between the store and dev-2's panels: it reads the flat signals once and
  // hands every panel PLAIN PROPS. No panel imports a store, so none of them can
  // disagree with the verdict about what the same read said.
  //
  // A null prop means the panel renders absent or sealed. It never means zero:
  // "0 Solari" under a wallet read that failed is a statement about somebody's
  // bank that we did not make.
  //
  // The wallet and the order book are PERSONAL and only mount for a linked
  // player. Rendered to a signed-out visitor they seal with "the bank could not
  // be read", which is a failure notice for a read nobody made; the storm and
  // the Landsraad are the public half and mount for everyone.
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LandsraadInstrument from '$lib/components/home/LandsraadInstrument.svelte';
  import OrdersInstrument from '$lib/components/home/OrdersInstrument.svelte';
  import RewardsStrip from '$lib/components/home/RewardsStrip.svelte';
  import SietchPresence from '$lib/components/home/SietchPresence.svelte';
  import StormInstrument from '$lib/components/home/StormInstrument.svelte';
  import WalletInstrument from '$lib/components/home/WalletInstrument.svelte';

  let { signals = null, linked = false, loading = false, homeStatus = 'loading' } = $props();

  let s = $derived(signals || {});

  // SietchPresence's own vocabulary: loading | ready | sealed | none. Derived
  // from the READ, not from the payload being empty: a composition read that
  // keeps failing leaves charName and guildName null forever, and a panel that
  // waits on those spins a loading state at a player who has a sietch.
  //
  // 'none' is the player in no guild, which the endpoint sends as the guild
  // object with a null name. A guild object that never arrived is 'sealed': we
  // could not read the sietch, which is not the same as not having one.
  let presenceStatus = $derived.by(() => {
    if (loading || homeStatus === 'loading') return 'loading';
    if (homeStatus === 'failed' || s.guildRead !== true) return 'sealed';
    return s.guildName ? 'ready' : 'none';
  });

  // DeliveriesCard's vocabulary: loading | ready | sealed | none. The count is
  // the read itself: null means the deliveries sub-object never arrived (the
  // game host was not answering), and 0 means it answered that nothing has been
  // sent. Those are two different sentences and only the second is 'none'.
  let deliveriesStatus = $derived.by(() => {
    if (loading || homeStatus === 'loading') return 'loading';
    if (homeStatus === 'failed' || s.deliveriesCount == null) return 'sealed';
    return s.deliveriesCount > 0 ? 'ready' : 'none';
  });
</script>

<div class="instruments">
  {#if linked}
    <WalletInstrument solari={s.bankSolari ?? null} />
    <OrdersInstrument
      marketOpen={s.marketOpen ?? null}
      marketFilled={s.marketFilled ?? null}
      karumOpen={s.karumOpen ?? null} />
  {/if}
  <StormInstrument storm={s.storm ?? null} mapLabel={s.mapLabel ?? null} now={s.now ?? null} />
  <LandsraadInstrument standings={s.standings ?? null} myContribution={s.myContribution ?? null} />
</div>

{#if linked}
  <div class="personal">
    <SietchPresence
      guildName={s.guildName ?? null}
      members={s.guildMembers ?? null}
      status={presenceStatus} />
    <RewardsStrip
      cycle={s.rewardsCycle ?? null}
      claimableTotal={s.claimableTotal ?? null}
      nextClaimUtc={s.nextClaimUtc ?? null}
      enabled={s.rewardsEnabled ?? null} />
    {#if deliveriesStatus === 'sealed'}
      <p class="delivery-note">Delivery status could not be read. <a href="/mailbox#deliveries">Check your packages</a>.</p>
    {:else if deliveriesStatus === 'ready'}
      <p class="delivery-note"><a href="/mailbox#deliveries">View your {s.deliveriesCount} {s.deliveriesCount === 1 ? 'package' : 'packages'}</a> and delivery details.</p>
    {/if}
  </div>
{:else}
  <div class="personal">
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see your own reading: your bank, your guild, your rewards and the invites waiting on you. The desert above is public either way." />
  </div>
{/if}

<style>
  .instruments {
    display: grid; gap: var(--space-4);
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    margin: 0 0 var(--space-5);
  }
  .personal { display: grid; gap: var(--space-4); grid-template-columns: 1fr; }
  @media (min-width: 860px) {
    .personal { grid-template-columns: 1.2fr 1fr; }
  }
  .delivery-note { grid-column: 1 / -1; margin: 0; font-size: 13px; color: var(--text-muted); }
</style>
