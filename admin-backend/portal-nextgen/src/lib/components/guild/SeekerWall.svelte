<script>
  // Solo LFG Seeker Wall: a live list of players raising a signal, plus the
  // viewer's own self-post form. account_id is never in the payload; char_name is
  // the display + DM key. The viewer's own row is detected by matching their linked
  // characters, so they can update it rather than message themselves.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import EliHint from './EliHint.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import SeekerCard from './SeekerCard.svelte';
  import RaiseSignalForm from './RaiseSignalForm.svelte';
  import { auth } from '$lib/auth.svelte.js';
  import { api } from '$lib/api.js';

  let seekers = $state([]);
  let status = $state('loading'); // 'loading' | 'ready' | 'error'

  let myNames = $derived(new Set((auth.linked || []).map((l) => l?.character_name).filter(Boolean)));
  let mine = $derived(seekers.find((s) => myNames.has(s?.char_name)) || null);
  let others = $derived(seekers.filter((s) => !myNames.has(s?.char_name)));
  let authed = $derived(auth.status === 'authed');

  async function load() {
    status = 'loading';
    try {
      const r = await api.guilds.lfgList();
      seekers = Array.isArray(r?.seekers) ? r.seekers : [];
      status = 'ready';
    } catch (e) {
      seekers = []; status = 'error';
    }
  }

  onMount(load);
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Seeker Wall | solo signals</p>
    {#if status === 'ready'}<span class="count mono">{seekers.length} up</span>{/if}
  </div>
  <EliHint text="Solo players looking for a guild. Post yourself or invite someone." />

  {#if authed}
    <div class="raise-slot">
      <RaiseSignalForm {mine} onChanged={load} />
    </div>
  {/if}

  {#if status === 'loading'}
    <p class="skeleton">loading</p>
  {:else if status === 'error'}
    <SealedPanel status="error" errorText="The wall could not be reached. Try again shortly." />
  {:else if others.length === 0}
    <SealedPanel
      status="empty" action="none" art="nobody-online"
      emptyText="No other signals are up right now. Raise yours and be the first."
    />
  {:else}
    <div class="grid">
      {#each others as s (s.char_name)}
        <SeekerCard seeker={s} canMessage={authed} />
      {/each}
    </div>
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .count { font-size: var(--text-xs); color: var(--text-muted); }
  .raise-slot { padding-bottom: var(--space-4); margin-bottom: var(--space-4); border-bottom: 1px solid var(--edge); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 18rem), 1fr)); gap: var(--space-3); }
</style>
