<script>
  import { untrack } from 'svelte';
  import { karum, fillRequest, loadSellable, uuidv4 } from '$lib/karum.svelte.js';
  import KarumItemSearch from './KarumItemSearch.svelte';
  import { gradeLabel, tierGradeLabel, wantedGradeLabel, gradeSatisfies } from './grade.js';
  import Modal from '$lib/components/Modal.svelte';

  let { request, onClose } = $props();

  let itemId = $state(null);
  let busy = $state(false);
  let phase = $state('idle');
  let message = $state('');
  let uuid = uuidv4();

  let price = $derived(Number(request?.price) || 0);
  let stack = $derived(Number(request?.stack_size) || 1);
  let locked = $derived(!karum.offlineOk);
  let undetermined = $derived(karum.online == null);
  // Template and stack size were always matched here. Grade is the third axis, added
  // 2026-08-25: offering a stack the order's grade rule rejects only produces a refusal
  // at the portal edge, so it is filtered out before the player can pick it.
  let sameItem = $derived((karum.sellable.items || []).filter(
    (item) => item.template === request.template_id && Number(item.stack_size || 1) === stack
  ));
  let matches = $derived(sameItem.filter(
    (item) => gradeSatisfies(item?.durability?.quality, request?.quality_level, request?.quality_mode)
  ));
  // Withheld purely on grade. Surfaced rather than dropped silently: the player can see
  // the stack in their own bank, and an unexplained absence reads as a bug. This mirrors
  // how the sell path already reports hidden_no_category.
  let hiddenByGrade = $derived(sameItem.length - matches.length);
  let wantedGrade = $derived(wantedGradeLabel(request?.quality_level, request?.quality_mode));
  let options = $derived(matches.map((item) => ({
    key: item.item_id,
    name: item.name || item.template,
    template: item.template,
    category: item.category || '',
    detail: `exact stack x${stack.toLocaleString()}${gradeLabel(item?.durability?.quality) ? ` | ${gradeLabel(item.durability.quality)}` : ''}${item.src === 'backpack' ? ' | backpack' : ''}`,
  })));
  let chosen = $derived(matches.find((item) => item.item_id === itemId) || null);
  let settled = $derived(phase === 'ok' || phase === 'inflight');
  let canFill = $derived(!busy && !settled && !locked && chosen != null);

  $effect(() => { untrack(loadSellable); });

  async function submit() {
    if (!canFill) return;
    busy = true;
    phase = 'idle';
    message = '';
    const result = await fillRequest({
      requestId: request.request_id,
      itemId: chosen.item_id,
      containerId: chosen?.container_id ?? karum.sellable.containerId,
      expectedPrice: price,
      expectedTemplate: request.template_id,
      uuid,
    });
    if (result.ok) {
      phase = 'ok';
      message = result.note || `You received ${price.toLocaleString()} Solari.`;
    } else if (result.inFlight) {
      phase = 'inflight';
      message = result.message;
    } else if (result.deferred) {
      phase = 'deferred';
      message = result.message;
    } else {
      phase = 'failed';
      message = result.message;
    }
    busy = false;
  }
</script>

<Modal title="Fill order · {request.display_name}" size="md" {onClose}>
  <dl class="facts">
    <div><dt class="mono">Requester</dt><dd>{request.requester_name || 'unknown'}</dd></div>
    <div><dt class="mono">Exact quantity</dt><dd class="mono">{stack.toLocaleString()}</dd></div>
    <div><dt class="mono">Grade wanted</dt><dd class="mono">{tierGradeLabel(request?.tier, null) ? `${tierGradeLabel(request.tier, null)} · ` : ''}{wantedGrade}</dd></div>
    <div><dt class="mono">You receive</dt><dd class="mono price">{price.toLocaleString()} Solari</dd></div>
  </dl>

  {#if !karum.flags.karum_wtb_enabled}
    <p class="soon" role="note">Wanted orders are not open yet. Nothing will move.</p>
  {/if}
  {#if locked}
    <p class="warn" role="note">
      {undetermined
        ? 'Cannot confirm you are logged out, so filling is locked.'
        : 'Log out of the game before filling this order.'}
      The item can only leave your bank or backpack while you are offline.
    </p>
  {/if}

  {#if karum.sellable.status === 'loading'}
    <p class="hollow mono">reading your bank and backpack</p>
  {:else if matches.length === 0}
    <p class="warn">
      No eligible stack found. This order requires one stack of exactly
      {stack.toLocaleString()} {request.display_name} at {wantedGrade}.
      {#if hiddenByGrade > 0}
        You hold {hiddenByGrade.toLocaleString()} matching stack{hiddenByGrade === 1 ? '' : 's'} of another grade.
      {/if}
    </p>
  {:else}
    <KarumItemSearch id="karum-fill-item" {options} selected={itemId}
                     onSelect={(value) => (itemId = value)} label="Matching stack from your bank or backpack"
                     placeholder="Choose the exact stack" disabled={locked || settled} />
    {#if hiddenByGrade > 0}
      <p class="note mono">
        {hiddenByGrade.toLocaleString()} more matching stack{hiddenByGrade === 1 ? '' : 's'} hidden: wrong grade for this order.
      </p>
    {/if}
  {/if}

  {#if request.funded === false}
    <p class="warn">The requester appeared low on funds at the last board refresh. The writer checks again before your item moves.</p>
  {/if}
  <p class="note">
    Your item and the requester's payment commit together. If the full balance is not
    available, both stay where they are. On success, the requester collects the item from
    the Completed tab at a CHOAM Exchange terminal, where it shows as CANCELED.
  </p>

  <div class="row">
    <button class="btn primary" type="button" onclick={submit} disabled={!canFill}>
      {busy ? 'Filling' : settled ? 'Done' : `Fill for ${price.toLocaleString()}`}
    </button>
    <button class="btn" type="button" onclick={() => onClose?.()}>{settled ? 'Close' : 'Cancel'}</button>
  </div>

  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
    {#if phase === 'inflight'}
      <p class="collect mono">do not submit again; the same fill is being reconciled</p>
    {/if}
  {/if}
</Modal>

<style>
  .facts { display: flex; flex-direction: column; gap: var(--space-1); margin: 0; }
  .facts > div { display: flex; justify-content: space-between; gap: var(--space-3); }
  .facts dt { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .facts dd { margin: 0; font-size: var(--text-sm); color: var(--text); }
  .facts dd.price { color: var(--accent-bright); font-variant-numeric: tabular-nums; }
  .note, .hollow, .soon { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.5; }
  .soon { color: var(--accent-text); }
  .warn { margin: 0; padding: var(--space-2); font-size: var(--text-xs); color: var(--text-muted); line-height: 1.45; border: 1px solid color-mix(in srgb, var(--ls-yellow) 45%, var(--edge)); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0)); }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:active { transform: translateY(1px); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); line-height: 1.5; }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='inflight'] { color: var(--ls-yellow); }
  .status[data-phase='deferred'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
  .collect { margin: 0; font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
</style>
