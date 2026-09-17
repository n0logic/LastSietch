<script>
  // Buy one listing. Modelled on bases/ImportDialog, the closest existing
  // "acquire this thing" flow: the shared Modal primitive owns the chrome, the
  // focus trap and Escape; the server envelope is surfaced honestly rather than
  // reinterpreted.
  //
  // ── THE THING THIS DIALOG EXISTS TO GET RIGHT ────────────────────────────────
  // A buy has FOUR outcomes, not two, and two of them are neither success nor
  // failure:
  //
  //   ok                paid and delivered
  //   in flight         `reconciling` (we do not yet know if the payment committed)
  //                     or `paid_undelivered` (it did; the goods are coming)
  //   deferred          the Karum is dark; nothing happened
  //   error             a clean refusal; you were not charged
  //
  // On IN FLIGHT the button does NOT re-arm. The server has accepted this
  // correlation_id and is resolving it; a second click would replay the same id,
  // which is harmless but tells the player to expect something a retry cannot give
  // them. Saying "we are confirming this" and stopping is the honest answer.
  //
  // The buyer also has to be told the item will show as CANCELED, because
  // completion_type 3 is the only format the game client renders and it will never
  // say "purchase". Without that line the trade looks broken when it worked.
  import { karum, buyListing, uuidv4 } from '$lib/karum.svelte.js';
  import { tierGradeLabel } from './grade.js';
  import Modal from '$lib/components/Modal.svelte';

  let { listing, onClose } = $props();

  let busy = $state(false);
  let phase = $state('idle');   // idle | ok | inflight | deferred | failed
  let message = $state('');
  let collectAt = $state('');

  let price = $derived(Number(listing?.price) || 0);
  let bank = $derived(typeof karum.bank === 'number' ? karum.bank : null);
  let affordable = $derived(bank == null || bank >= price);
  let after = $derived(bank == null ? null : bank - price);
  // Settled means done with, either way: nothing further to click.
  let settled = $derived(phase === 'ok' || phase === 'inflight');
  let canBuy = $derived(!busy && !settled && affordable);

  // One key for this intended purchase, reused if the player retries after a lost
  // response. The game DB dedupes on it, so a replay cannot double-charge.
  let uuid = uuidv4();

  async function submit() {
    if (!canBuy) return;
    busy = true; phase = 'idle'; message = '';
    const r = await buyListing({
      listingId: listing.listing_id, expectedPrice: price, uuid,
    });
    if (r.ok) {
      phase = 'ok';
      collectAt = r.collectAt;
      message = r.note || 'Paid. Collect it from the Completed tab at any CHOAM Exchange terminal.';
    } else if (r.inFlight) {
      phase = 'inflight';
      message = r.message;
    } else if (r.deferred) {
      phase = 'deferred';
      message = r.message;
    } else {
      phase = 'failed';
      message = r.message;
    }
    busy = false;
  }
</script>

<Modal title="Buy · {listing.display_name || listing.template_id}" size="md" {onClose}>
  <dl class="facts">
    <div><dt class="mono">Seller</dt><dd>{listing.seller_name || 'unknown'}</dd></div>
    <div><dt class="mono">Quantity</dt><dd class="mono">{(Number(listing.stack_size) || 1).toLocaleString()}</dd></div>
    {#if Number(listing.quality_level) > 0}
      <div><dt class="mono">Grade</dt><dd class="mono">{tierGradeLabel(listing.tier, listing.quality_level)}</dd></div>
    {/if}
    <div><dt class="mono">Price</dt><dd class="mono price">{price.toLocaleString()} Solari</dd></div>
    {#if bank != null}
      <div>
        <dt class="mono">Your bank</dt>
        <dd class="mono">{bank.toLocaleString()}{after != null && affordable && phase === 'idle' ? ` → ${after.toLocaleString()}` : ''}</dd>
      </div>
    {/if}
  </dl>

  {#if !karum.flags.karum_enabled}
    <p class="soon" role="note">The Karum is not open yet. Nothing will move.</p>
  {/if}

  {#if !affordable}
    <p class="warn" role="note">You do not have enough banked Solari for this.</p>
  {:else if phase === 'idle'}
    <p class="note">
      Paid from your bank balance. The item then waits for you in the
      <strong>Completed</strong> tab at any CHOAM Exchange terminal, where it shows as
      <strong>CANCELED</strong> &mdash; that is the only way the game can display a
      collected item, and it is normal.
    </p>
  {/if}

  <div class="row">
    <button class="btn primary" type="button" onclick={submit} disabled={!canBuy}>
      {busy ? 'Paying' : settled ? 'Done' : `Buy for ${price.toLocaleString()}`}
    </button>
    <button class="btn" type="button" onclick={() => onClose?.()}>
      {settled ? 'Close' : 'Cancel'}
    </button>
  </div>

  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
    {#if phase === 'ok' && collectAt}
      <p class="collect mono">collect at {collectAt}</p>
    {/if}
    {#if phase === 'inflight'}
      <p class="collect mono">do not buy again; refresh in a moment to see where it landed</p>
    {/if}
  {/if}
</Modal>

<style>
  .facts { display: flex; flex-direction: column; gap: var(--space-1); margin: 0; }
  .facts > div { display: flex; justify-content: space-between; gap: var(--space-3); }
  .facts dt { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .facts dd { margin: 0; font-size: var(--text-sm); color: var(--text); }
  .facts dd.price { color: var(--accent-bright); font-variant-numeric: tabular-nums; }
  .note { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.5; }
  .note strong { color: var(--text); }
  .soon { margin: 0; font-size: var(--text-xs); color: var(--accent-text); }
  .warn {
    margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.4;
    padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--ls-yellow) 45%, var(--edge));
    background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0));
  }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); line-height: 1.5; }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='inflight'] { color: var(--ls-yellow); }
  .status[data-phase='deferred'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
  .collect { margin: 0; font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
</style>
