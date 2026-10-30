<script>
  // Sietch > Sietches (P3, owner 2026-10-30): one page per sietch, as tabs.
  // Habbanya is the landing view; Amtal is `?tab=amtal` and carries the shipped
  // Amtal page unchanged. The tab strip itself is the shell's (navNext.js).
  //
  // New with the redesign, so it exists only behind the shell switch: a viewer on
  // the shipped shell is sent to the Amtal page they already know. The Amtal tab
  // keeps the shipped nav's gate (the `amtal` feature); a gated or unknown ?tab=
  // falls back to Habbanya, the same rule the tab strip follows.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { api } from '$lib/api.js';
  import { shell, loadShell } from '$lib/shellNext.svelte.js';
  import HabbanyaGuide from '$lib/components/sietches/HabbanyaGuide.svelte';
  import AmtalPage from '../amtal/+page.svelte';

  let features = $state(null);
  onMount(() => {
    if (shell.status !== 'ready') loadShell();
    api.server.features().then((d) => { features = (d && d.features) || {}; }).catch(() => { features = {}; });
  });

  $effect(() => {
    if (shell.status === 'ready' && !shell.next) goto(`${base}/amtal`, { replaceState: true });
  });

  let asked = $derived($page.url.searchParams.get('tab'));
  let amtal = $derived(asked === 'amtal' && features?.amtal === true);
</script>

<svelte:head>
  <title>{amtal ? 'Amtal' : 'Habbanya'} | Sietches | Last Sietch</title>
</svelte:head>

<div class="page">
  {#if shell.status !== 'ready' || !shell.next || features === null}
    <p class="skeleton">loading</p>
  {:else if amtal}
    <AmtalPage />
  {:else}
    <HabbanyaGuide />
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
</style>
