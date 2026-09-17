<script>
  // Character: the per-character dossier re-skinned in the carved-sietch language.
  // TOP = an identity nameplate (name, level, faction, online + current map).
  // Then a two-column stack of carved slabs: vitals + faction standing + spec
  // tracks on the left, journey + Landsraad teaser + equipped LIST on the right.
  // The 3D gear stage is DEFERRED (contract owner decision #1) — equipped ships as
  // a list card. V2 honors the selected character (unlike V1's account default).
  //
  // Read-only surface. Reads are never gated; signed-out seals to an honest
  // Connect-Discord panel. Sections that could not resolve for a non-default
  // selected controller show a subtle "reflects last logout" note.
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import { auth } from '$lib/auth.svelte.js';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { character, loadAll } from '$lib/character.svelte.js';
  import { factionCrestUrl } from '$lib/icons.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import VitalsCard from '$lib/components/character/VitalsCard.svelte';
  import FactionStandingCard from '$lib/components/character/FactionStandingCard.svelte';
  import SpecializationTracks from '$lib/components/character/SpecializationTracks.svelte';
  import JourneyCard from '$lib/components/character/JourneyCard.svelte';
  import LandsraadTeaser from '$lib/components/character/LandsraadTeaser.svelte';
  import EquippedList from '$lib/components/character/EquippedList.svelte';

  const gate = useAuthGate();

  let hdr = $derived(character.header);
  let crest = $derived(hdr ? factionCrestUrl(hdr.faction_crest || hdr.faction) : null);
  // Vitals + equipped are RAM-cached, so surface the "last logout" note for a
  // non-default selected character (equipped.ctrl_scoped:false) or always as a
  // gentle reminder that balances lag a live session.
  let vitalsLastLogout = $derived(character.equipped?.ctrl_scoped === false);

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadAll); }
    else if (status === 'anon') { loaded = false; }
  });
</script>

<svelte:head>
  <title>Character | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Character dossier"
    title="Character"
    sub="Your vitals, faction standing, specialization tracks, journey so far, and the gear you have equipped. This reflects the character selected in the top bar."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see your character vitals, standing, and equipped gear."
    />
  {:else if character.status === 'error'}
    <SealedPanel
      status="error"
      errorText="Your character could not be read right now. Try again in a moment."
    />
  {:else}
    <!-- Identity nameplate -->
    <CarvedSlab>
      <div class="nameplate">
        {#if crest}<img class="crest" src={crest} alt="" aria-hidden="true" loading="lazy" />{/if}
        <div class="ident">
          <span class="cname">{hdr?.name || character.characterName || 'Character'}</span>
          <span class="cmeta mono">
            {#if hdr?.level != null}<span>Level {hdr.level}</span>{/if}
            {#if hdr?.faction}<span>{hdr.faction}</span>{/if}
          </span>
        </div>
        <div class="presence">
          {#if hdr?.online}
            <span class="on"><LiveDot /> Online{#if hdr?.current_map}<span class="map"> · {hdr.current_map}</span>{/if}</span>
          {:else}
            <span class="off mono">Offline{#if hdr?.last_online}<span class="map"> · seen {hdr.last_online}</span>{/if}</span>
          {/if}
        </div>
      </div>
    </CarvedSlab>

    <div class="grid">
      <!-- LEFT: currency + standing + specs -->
      <div class="col left">
        <CarvedSlab sharp={true}><VitalsCard vitals={character.vitals} lastLogout={vitalsLastLogout} /></CarvedSlab>
        <CarvedSlab><FactionStandingCard rep={character.factionRep} /></CarvedSlab>
        <CarvedSlab sharp={true}><SpecializationTracks specializations={character.specializations} /></CarvedSlab>
      </div>

      <!-- RIGHT: journey + landsraad teaser + equipped -->
      <div class="col right">
        <CarvedSlab><JourneyCard journey={character.journey} /></CarvedSlab>
        <CarvedSlab><LandsraadTeaser teaser={character.landsraadTeaser} /></CarvedSlab>
        <CarvedSlab sharp={true}><EquippedList equipped={character.equipped} /></CarvedSlab>

        <!-- Your data: backup reassurance. The keepsake export moved to Settings
             in wave 4; the pointer keeps the same server flag so it never offers
             a download the backend refuses. -->
        <CarvedSlab>
          <div class="yourdata">
            <h3 class="yd-title">Your Data</h3>
            <p class="yd-safe">Your character is saved on the server and backed up every 6 hours. If the server ever goes down for maintenance, your progress is safe.</p>
            {#if auth.exportEnabled}
              <p class="yd-note">A keepsake copy of this dossier is yours to download from <a href={`${base}/settings`}>Settings</a>.</p>
            {/if}
          </div>
        </CarvedSlab>
      </div>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .nameplate { display: flex; align-items: center; gap: var(--space-4); flex-wrap: wrap; }
  .crest { width: 52px; height: 52px; object-fit: contain; flex: 0 0 auto; }
  .ident { display: flex; flex-direction: column; min-width: 0; }
  .cname { font-family: var(--font-display); font-size: var(--text-2xl); letter-spacing: .04em; text-transform: uppercase; color: var(--text); }
  .cmeta { display: flex; gap: var(--space-3); font-size: var(--text-sm); color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; }
  .presence { margin-left: auto; font-size: var(--text-sm); }
  .on { display: inline-flex; align-items: center; gap: var(--space-2); color: var(--ls-ibad); }
  .off { color: var(--text-muted); }
  .map { color: var(--text-muted); }

  .grid {
    margin-top: var(--space-4);
    display: grid; gap: var(--space-4);
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    align-items: start;
  }
  .col { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }

  .yourdata { display: flex; flex-direction: column; gap: var(--space-3); }
  .yd-title { font-family: var(--font-display); font-size: var(--text-lg); letter-spacing: .06em; text-transform: uppercase; color: var(--text); margin: 0; }
  .yd-safe { color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5; margin: 0; }
  .yd-note { color: var(--text-muted); font-size: var(--text-xs); line-height: 1.5; margin: 0; }
  .yd-note a { color: var(--accent-text); }

  /* Stagger-rise the panels on enter (opacity/transform only). */
  .col > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .col.left > :global(*:nth-child(1)) { animation-delay: .06s; }
  .col.left > :global(*:nth-child(2)) { animation-delay: .12s; }
  .col.left > :global(*:nth-child(3)) { animation-delay: .18s; }
  .col.right > :global(*:nth-child(1)) { animation-delay: .09s; }
  .col.right > :global(*:nth-child(2)) { animation-delay: .15s; }
  .col.right > :global(*:nth-child(3)) { animation-delay: .21s; }
  @keyframes rise { to { opacity: 1; transform: none; } }

  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
  }
  @media (prefers-reduced-motion: reduce) {
    .col > :global(*) { opacity: 1; transform: none; animation: none; }
  }
</style>
