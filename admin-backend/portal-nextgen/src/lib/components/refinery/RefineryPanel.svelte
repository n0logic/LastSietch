<script>
  // Ingot Refinery: hand in base refined ingots plus Spice Melange, take out the
  // matching Spice-infused metal dust. A CHOAM service, not a bench recipe: the
  // dusts are loot-only in the game and nothing in it turns an ingot into one.
  //
  // FOUR HONEST STATES, all of them caller-supplied copy on SealedPanel:
  //   loading  the catalog read has not landed
  //   anon     no session, so there is nothing of the player's to read
  //   closed   the refinery flag is off. "Not open yet" is a QUIET state: no
  //            toast, no disabled controls to poke, and never a fake success
  //   sealed   the read FAILED. Deliberately not the same state as closed:
  //            "not open yet" is a claim about the service, and a failed read
  //            does not tell us that
  //
  // The write surface is offline-gated the way storage moves and repairs are.
  // Undetermined counts as LOCKED: a take from a session the engine still holds
  // open is rewritten with the original row at logout, which is real duplication.
  // The server gates again at the route and a third time inside the writer under
  // locks; this is honest UI, not the security boundary.
  import { onMount, untrack } from 'svelte';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { refinery, loadCatalog, subscribe } from '$lib/refinery.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import RefineryRow from '$lib/components/refinery/RefineryRow.svelte';

  const gate = useAuthGate();

  onMount(() => subscribe());

  // subscribe() fires its first read immediately, which is a no-op while the
  // session is still resolving. This is the read that actually lands.
  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadCatalog); }
    else if (status === 'anon') { loaded = false; }
  });

  let locked = $derived(!refinery.offlineOk);
  let undetermined = $derived(refinery.online == null);
  let booting = $derived(refinery.status === 'idle' || refinery.status === 'loading');
</script>

{#if gate.loading || (gate.authed && booting)}
  <SealedPanel status="loading" loadingText="reading the refinery" slab={true} />
{:else if gate.anon}
  <SealedPanel
    status="empty" action="login" slab={true}
    emptyText="Sign in to the portal to use the Ingot Refinery."
  />
{:else if refinery.status === 'error'}
  <SealedPanel
    status="error" slab={true} width="prose"
    errorText="The refinery could not be read right now. Nothing was taken. Try again in a moment."
  />
{:else if !refinery.enabled}
  <SealedPanel
    status="empty" slab={true} width="prose"
    emptyText="The Ingot Refinery is not open yet."
  />
{:else}
  <CarvedSlab sharp={true}>
    <div class="wrap">
      <div class="gate" class:locked data-state={locked ? 'locked' : 'open'} role="status" aria-live="polite">
        <LiveDot tone={undetermined ? 'idle' : refinery.online ? 'warn' : 'live'} />
        <span class="msg">
          {#if undetermined}
            Checking your character. Refining stays locked until we can confirm you are logged out.
          {:else if refinery.online}
            Log out of the game first, then refine. The refinery only works on storage nobody is holding open.
          {:else}
            You are logged out{refinery.characterName ? ` as ${refinery.characterName}` : ''}. The refinery is open.
          {/if}
        </span>
      </div>

      <p class="lede">
        Refine base ingots and Spice Melange into matching Spice-infused dust. Inputs come from your
        CHOAM bank, your backpack and your toolbar; the dust always lands in your bank. Each tier
        has its own weekly allowance, and spending one leaves the other five untouched.
      </p>

      {#if refinery.recipes.length === 0}
        <SealedPanel
          status="empty" width="prose"
          emptyText="No refining rates are published right now. Nothing was taken."
        />
      {:else}
        <table class="rates">
          <caption>Refinery rates per batch <span>Current published recipes. Ingots match the dust tier.</span></caption>
          <thead><tr><th scope="col">Dust tier</th><th scope="col">Ingots spent</th><th scope="col">Melange spent</th><th scope="col">Dust received</th></tr></thead>
          <tbody>
            {#each refinery.recipes as recipe (recipe.output_template)}
              <tr class:paused={!recipe.enabled}>
                <th scope="row">T{recipe.tier} · {recipe.output_name.replace(/^Spice-infused /, '').replace(/ Dust$/, '')}{#if !recipe.enabled}<small>Paused</small>{/if}</th>
                <td>{recipe.input_per_batch}</td>
                <td>{recipe.spice_per_batch}</td>
                <td>{recipe.output_per_batch}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="lede">The quantities above are for one whole batch. Your remaining allowance and available materials are shown below. An allowance smaller than one batch cannot be used until more becomes available.</p>
        <div class="tiers">
          {#each refinery.recipes as recipe (recipe.output_template)}
            <RefineryRow {recipe} {locked} />
          {/each}
        </div>
      {/if}
    </div>
  </CarvedSlab>
{/if}

<style>
  .wrap { display: flex; flex-direction: column; gap: var(--space-4); }

  .gate {
    display: flex; align-items: center; gap: var(--space-3);
    padding: var(--space-2) var(--space-4); border-radius: var(--radius-sm);
    border: 1px solid var(--edge); background: var(--metal-0); font-size: var(--text-sm);
  }
  .gate.locked {
    border-color: color-mix(in srgb, var(--ls-yellow) 45%, var(--edge));
    background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0));
  }
  .gate .msg { color: var(--text-muted); }

  .lede { margin: 0; color: var(--text-muted); font-size: var(--text-sm); max-width: 68ch; line-height: 1.55; }

  .rates { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  .rates caption { text-align: left; color: var(--text); font-weight: 500; padding-bottom: var(--space-3); }
  .rates caption span { display: block; color: var(--text-muted); font-weight: 400; margin-top: var(--space-1); }
  .rates th, .rates td { padding: var(--space-3) var(--space-2); border-bottom: 1px solid var(--edge); text-align: right; }
  .rates th { font-weight: 500; }
  .rates th:first-child { text-align: left; }
  .rates thead th { color: var(--text-muted); font-size: var(--text-xs); }
  .rates td { font-variant-numeric: tabular-nums; }
  .rates small { display: block; color: var(--text-muted); }
  .rates .paused { color: var(--text-muted); }
  @media (max-width: 480px) {
    .rates { font-size: 12px; table-layout: fixed; }
    .rates th, .rates td { padding: 10px 4px; overflow-wrap: anywhere; }
    .rates th:first-child { width: 35%; }
  }

  .tiers { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--space-3); }
  @media (min-width: 900px) { .tiers { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
