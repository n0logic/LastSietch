<script>
  // Moved. The augmented-gear roll-up is now the Augments tab of Storage >
  // Workshop, alongside the Ingot Refinery. This route stays a redirect rather
  // than disappearing: it is the link in the Storage submenu players have
  // bookmarked, it is in Discord scrollback, and a dead URL would read as the
  // feature having been removed.
  //
  // replaceState so the Back button leaves the portal the way it would have from
  // the old page, instead of bouncing through this route again.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';

  const TARGET = `${base}/storage/workshop?tab=augments`;

  onMount(() => { goto(TARGET, { replaceState: true }); });
</script>

<svelte:head>
  <title>Augmented Gear | Last Sietch</title>
</svelte:head>

<div class="page">
  <p class="moved">Augmented Gear has moved to <a href={TARGET}>Storage &gt; Workshop</a>.</p>
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .moved { color: var(--text-muted); font-size: var(--text-sm); }
  .moved a { color: var(--accent-text); }
</style>
