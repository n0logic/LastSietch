<script>
  // One tier of the Ingot Refinery: the rate, what the player holds for it across
  // all three sources, what the week has left, and the confirm that spends it.
  //
  // Every figure on this row is a number the SERVER summed. The toolbar never
  // appears in the container browser, so a client that tried to add these up
  // itself would either be wrong or would fan out a request per source, which is
  // the shape that hung the storage grid on 2026-08-02.
  //
  // The confirm is TYPED, not a single click. A batch consumes real ingots and
  // real melange out of three inventories at once and the result is not visible
  // in game until the player relogs, so a misclick is expensive to understand
  // and impossible to undo.
  //
  // A tier switched off in the rate table renders as a quiet closed state. It is
  // not a failure and it is not the player's doing, so it gets no notice, no
  // toast and no disabled button to poke at.
  import { refinery, maxBatchesFor, submitExchange, clearResult, unreadReason } from '$lib/refinery.svelte.js';
  import { iconUrl } from '$lib/icons.js';
  import Modal from '$lib/components/Modal.svelte';
  import Notice from '$lib/components/Notice.svelte';

  let { recipe, locked = false } = $props();


  let batches = $state(1);
  let confirmOpen = $state(false);
  // A checkbox, not a typed word (owner ruling 2026-09-04 after the first live
  // trade): the trade is small, all-or-nothing, and the dialog already restates it.
  let confirmed = $state(false);

  let read = $derived(refinery.holdingsRead);
  // Why the stock is unread, when the host said. The row stays locked either way:
  // canRefine below gates on `read`, not on the reason.
  let unreadText = $derived(unreadReason());
  let ceiling = $derived(maxBatchesFor(recipe));
  let busy = $derived(refinery.busy === recipe.output_template);
  // Clamped for display only. `batches` itself is re-clamped on every change and
  // again before the write, so a catalog refresh that lowers the ceiling mid-page
  // cannot leave a larger number sitting in the confirm.
  let n = $derived(Math.max(1, Math.min(batches, Math.max(1, ceiling))));

  let needInput = $derived(n * recipe.input_per_batch);
  let needSpice = $derived(n * recipe.spice_per_batch);
  let getsOut = $derived(n * recipe.output_per_batch);

  // An unread stock locks the row exactly as being online does. We cannot say
  // what the player holds, so we must not offer to spend it.
  let canRefine = $derived(!locked && !busy && read && recipe.enabled && ceiling >= 1);

  // The result belongs to THIS tier only. Six rows share one store slot, and a
  // T3 refine must not paint its outcome across the T5 row.
  let result = $derived(
    refinery.result && refinery.result.output_template === recipe.output_template
      ? refinery.result
      : null
  );
  let notice = $derived(
    refinery.noticeFor === recipe.output_template && refinery.notice ? refinery.notice : ''
  );

  // The consumed block is keyed by template id; the player reads names.
  let consumedRows = $derived(
    Object.entries((result && result.consumed) || {}).map(([template, c]) => ({
      template,
      name:
        template === recipe.input_template
          ? recipe.input_name
          : template === recipe.spice_template
            ? recipe.spice_name
            : template,
      units: Number(c?.units) || 0,
      from: {
        bank: Number(c?.from?.bank) || 0,
        backpack: Number(c?.from?.backpack) || 0,
        toolbar: Number(c?.from?.toolbar) || 0,
      },
    }))
  );

  let grantedStacks = $derived(
    Array.isArray(result && result.granted && result.granted.stacks) ? result.granted.stacks : []
  );

  function fmt(v) {
    return (Number(v) || 0).toLocaleString();
  }

  function setBatches(v) {
    const raw = Math.floor(Number(v) || 0);
    batches = Math.max(1, Math.min(raw, Math.max(1, ceiling)));
  }

  function openConfirm() {
    if (!canRefine) return;
    confirmed = false;
    confirmOpen = true;
  }

  async function confirm() {
    if (!confirmed) return;
    confirmOpen = false;
    confirmed = false;
    await submitExchange(recipe, n);
  }
</script>

<article class="tier" class:closed={!recipe.enabled}>
  <header class="head">
    <img class="ico" src={iconUrl(recipe.output_icon)} alt="" aria-hidden="true" />
    <div class="names">
      <p class="tname">{recipe.output_name}</p>
      <p class="kicker mono">Tier {recipe.tier}</p>
    </div>
    {#if read}
      <p class="held mono">
        <span class="lbl">held</span>
        <span class="own">{fmt(recipe.held_output)}</span>
      </p>
    {/if}
  </header>

  <p class="rate mono">
    <img class="mini" src={iconUrl(recipe.input_icon)} alt="" aria-hidden="true" />
    {recipe.input_per_batch} x {recipe.input_name}
    + {#if recipe.spice_icon}<img class="mini" src={iconUrl(recipe.spice_icon)} alt="" aria-hidden="true" />{/if}{recipe.spice_per_batch} x {recipe.spice_name}
    = {recipe.output_per_batch} x {recipe.output_name}
  </p>

  {#if !recipe.enabled}
    <p class="quiet">That tier is not open yet. The other tiers still refine.</p>
  {:else}
    {#if !read}
      <p class="unread">{unreadText}</p>
    {:else}
    <div class="holds-scroll" tabindex="0" role="region" aria-label="Inventory sources for {recipe.output_name}">
    <table class="holds">
      <thead>
        <tr>
          <th scope="col">You hold</th>
          <th scope="col">Bank</th>
          <th scope="col">Backpack</th>
          <th scope="col">Toolbar</th>
          <th scope="col">Total</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <th scope="row">{recipe.input_name}</th>
          <td class="mono own">{fmt(recipe.held_input.bank)}</td>
          <td class="mono own">{fmt(recipe.held_input.backpack)}</td>
          <td class="mono own">{fmt(recipe.held_input.toolbar)}</td>
          <td class="mono own tot">{fmt(recipe.held_input.total)}</td>
        </tr>
        <tr>
          <th scope="row">{recipe.spice_name}</th>
          <td class="mono own">{fmt(recipe.held_spice.bank)}</td>
          <td class="mono own">{fmt(recipe.held_spice.backpack)}</td>
          <td class="mono own">{fmt(recipe.held_spice.toolbar)}</td>
          <td class="mono own tot">{fmt(recipe.held_spice.total)}</td>
        </tr>
      </tbody>
    </table>
    </div>
    {/if}

    <p class="week">
      This week: <span class="mono own">{fmt(recipe.weekly_dust_used)}</span> of
      <span class="mono">{fmt(recipe.weekly_dust_cap)}</span> refined,
      <span class="mono own">{fmt(recipe.weekly_dust_left)}</span> left for this tier.
      The other tiers keep their own allowance.
    </p>

    {#if result}
      <section class="result" aria-live="polite">
        <p class="kicker mono">Refined</p>
        {#if result.message}<p class="msg">{result.message}</p>{/if}
        {#if result.replay}
          <p class="msg">This request had already been applied, so nothing was taken a second time.</p>
        {/if}
        <ul class="lines mono">
          {#each consumedRows as c (c.template)}
            <li>
              Took <span class="own">{fmt(c.units)}</span> {c.name}
              (bank {fmt(c.from.bank)}, backpack {fmt(c.from.backpack)}, toolbar {fmt(c.from.toolbar)})
            </li>
          {/each}
          {#each grantedStacks as s (s.item_id)}
            <li>
              {s.merged ? 'Merged' : 'Placed'} <span class="own">{fmt(s.stack_size)}</span>
              {recipe.output_name} in bank slot {s.slot}
            </li>
          {/each}
        </ul>
        <button class="btn" type="button" onclick={clearResult}>Refine more</button>
      </section>
    {:else}
      <div class="controls">
        <label class="stepper">
          <span class="lbl">Batches</span>
          <button
            class="step"
            type="button"
            onclick={() => setBatches(n - 1)}
            disabled={!canRefine || n <= 1}
            aria-label="One batch fewer">&minus;</button>
          <input
            class="mono"
            type="number"
            min="1"
            max={Math.max(1, ceiling)}
            value={n}
            disabled={!canRefine}
            oninput={(e) => setBatches(e.currentTarget.value)}
          />
          <button
            class="step"
            type="button"
            onclick={() => setBatches(n + 1)}
            disabled={!canRefine || n >= ceiling}
            aria-label="One batch more">+</button>
        </label>
        {#if read}
          <p class="afford mono">
            Enough for <span class="own">{fmt(ceiling)}</span>
            {ceiling === 1 ? 'batch' : 'batches'} right now
          </p>
        {/if}
        <button class="btn primary" type="button" onclick={openConfirm} disabled={!canRefine}>
          {busy ? 'Refining' : 'Refine'}
        </button>
      </div>

      <p class="order">
        Drawn from your bank first, then backpack, then toolbar. The dust lands in your bank.
      </p>
    {/if}

    {#if notice}
      <Notice tone={refinery.noticeTone} text={notice} />
    {/if}
  {/if}
</article>

{#if confirmOpen}
  <Modal title="Refine {recipe.output_name}" size="md" onClose={() => (confirmOpen = false)}>
    <p class="cline">
      Refining <span class="mono own">{fmt(n)}</span> {n === 1 ? 'batch' : 'batches'} takes
      <span class="mono own">{fmt(needInput)}</span> {recipe.input_name} and
      <span class="mono own">{fmt(needSpice)}</span> {recipe.spice_name}, and makes
      <span class="mono own">{fmt(getsOut)}</span> {recipe.output_name}.
    </p>
    <p class="cline">
      Drawn from your bank first, then backpack, then toolbar. The dust lands in your bank.
      You will not see it in game until you log back in.
    </p>
    <label class="chk">
      <input type="checkbox" bind:checked={confirmed} />
      <span>I understand these materials leave my storage now and the dust waits in my bank.</span>
    </label>
    <div class="crow">
      <button
        class="btn primary"
        type="button"
        onclick={confirm}
        disabled={!confirmed || !canRefine}>Refine</button>
      <button class="btn" type="button" onclick={() => (confirmOpen = false)}>Cancel</button>
    </div>
  </Modal>
{/if}

<style>
  .tier {
    display: flex; flex-direction: column; gap: var(--space-3);
    min-width: 0;
    padding: var(--space-4); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); background: var(--metal-0);
  }
  .tier.closed { opacity: .72; }

  .head { display: flex; align-items: center; gap: var(--space-3); }
  .ico { width: 40px; height: 40px; image-rendering: auto; flex: none; }
  .mini { width: 18px; height: 18px; vertical-align: -4px; margin-right: var(--space-1); }
  .names { min-width: 0; }
  .tname { margin: 0; font-size: var(--text-md); line-height: 1.2; }
  .kicker { margin: 2px 0 0; }
  .head .held { margin: 0 0 0 auto; text-align: right; font-size: var(--text-sm); }
  .lbl {
    display: block; color: var(--text-muted); font-size: var(--text-xs);
    text-transform: uppercase; letter-spacing: .1em;
  }

  /* Owned figures are AMBER. Ibad blue on this page belongs to the online dot
     and nothing else. */
  .own { color: var(--accent-text); }

  .rate {
    margin: 0; font-size: var(--text-sm); color: var(--text);
    padding: var(--space-2) var(--space-3);
    background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }

  .quiet { margin: 0; color: var(--text-muted); font-size: var(--text-sm); }
  /* Unread stock. Muted, not red: nothing failed for the player and nothing was
     taken, we simply have no figure to show them. */
  .unread { margin: 0; color: var(--text-muted); font-size: var(--text-sm); }

  .holds { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  .holds-scroll { max-width: 100%; overflow-x: auto; }
  .holds-scroll:focus-visible { outline: 1px solid var(--accent); outline-offset: 2px; }
  .holds th, .holds td { padding: var(--space-1) var(--space-2); text-align: right; }
  .holds thead th {
    color: var(--text-muted); font-size: var(--text-xs); font-weight: 400;
    text-transform: uppercase; letter-spacing: .08em; border-bottom: 1px solid var(--edge);
  }
  .holds tbody th { text-align: left; font-weight: 400; color: var(--text-muted); }
  .holds thead th:first-child { text-align: left; }
  .holds .tot { border-left: 1px solid var(--edge); }

  .week, .order { margin: 0; color: var(--text-muted); font-size: var(--text-xs); line-height: 1.5; }

  .controls { display: flex; align-items: flex-end; gap: var(--space-3); flex-wrap: wrap; }
  .stepper { display: flex; align-items: center; gap: var(--space-1); }
  .stepper .lbl { align-self: center; margin-right: var(--space-2); }
  .stepper input {
    width: 5rem; text-align: center; font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1);
  }
  .step {
    width: 2rem; height: 2rem; line-height: 1; font-size: var(--text-md);
    color: var(--text); background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); cursor: pointer;
  }
  .afford { margin: 0 auto 0 0; font-size: var(--text-xs); color: var(--text-muted); }

  .btn {
    font-family: var(--font-display); letter-spacing: .06em; font-size: var(--text-sm);
    padding: var(--space-1) var(--space-4); cursor: pointer;
    color: var(--text); background: var(--panel);
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn:not(:disabled):hover, .step:not(:disabled):hover { border-color: var(--accent); }

  .result {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3); border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--ls-green) 40%, var(--edge));
    background: color-mix(in srgb, var(--ls-green) 7%, var(--metal-0));
  }
  .result .btn { align-self: flex-start; }
  .msg { margin: 0; font-size: var(--text-sm); }
  .lines { margin: 0; padding-left: var(--space-4); font-size: var(--text-xs); color: var(--text-muted); }
  .lines li { line-height: 1.6; }

  .cline { margin: 0; font-size: var(--text-sm); line-height: 1.5; }
  .fld {
    display: flex; flex-direction: column; gap: var(--space-1);
    font-size: var(--text-xs); color: var(--text-muted);
    text-transform: uppercase; letter-spacing: .06em;
  }
  .fld input {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    text-transform: none; letter-spacing: normal;
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2);
  }
  .fld input:focus-visible, .stepper input:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  .crow { display: flex; gap: var(--space-2); }
  .chk { display: flex; align-items: flex-start; gap: var(--space-2); margin: var(--space-3) 0; font-size: var(--text-sm); color: var(--text); cursor: pointer; }
  .chk input { margin-top: 3px; accent-color: var(--accent); }
</style>
