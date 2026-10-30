<script>
  // Confirm one Stall purchase. The idempotency key is minted ONCE when the dialog
  // opens and reused if the player retries, so a lost response cannot buy twice.
  // Game goods ask which character receives them when the account has several;
  // the server resolves and re-checks the controller either way.
  import Modal from '$lib/components/Modal.svelte';
  import { auth } from '$lib/auth.svelte.js';
  import { uuidv4 } from '$lib/api.js';
  import { order } from '$lib/sietch.svelte.js';
  import { COPY } from './copy.js';

  let { item, balance = 0, onClose, onDone } = $props();

  const idemKey = uuidv4();
  let busy = $state(false);
  let error = $state('');
  let game = $derived(item.lane === 'bank' || item.lane === 'wallet' || item.lane === 'spec' || item.lane === 'charxp');
  let chars = $derived((auth.characters || []).filter((c) => c && c.controller_id));
  let pick = $state((auth.characters || []).find((c) => c?.selected)?.controller_id || (auth.characters || [])[0]?.controller_id || null);

  async function buy() {
    busy = true;
    error = '';
    const r = await order({ sku: item.sku, idemKey, characterId: game ? pick : null });
    busy = false;
    if (r.ok) onDone(r);
    else error = r.message;
  }
</script>

<Modal title={`Buy ${item.name}`} size="md" {onClose}>
  <p class="cost">
    <span class="mono price">{item.price}</span> {item.price === 1 ? 'LS Token' : COPY.event.currency}
    <span class="after">You will have {balance - item.price} left.</span>
  </p>
  {#if item.blurb}<p class="blurb">{item.blurb}</p>{/if}

  {#if game && chars.length > 1}
    <fieldset>
      <legend>Deliver to</legend>
      {#each chars as c (c.controller_id)}
        <label><input type="radio" name="sn-char" value={c.controller_id} bind:group={pick} /> {c.char_name}</label>
      {/each}
    </fieldset>
  {/if}
  {#if item.lane === 'bank'}
    <p class="note">{COPY.stall.bank_note} A character that has never logged in has no bank yet: log in once first.</p>
  {:else if item.lane === 'wallet'}
    <p class="note">The Solari land in your wallet. Relog if you do not see them.</p>
  {:else if item.lane === 'spec'}
    <p class="note">{COPY.stall.spec_note}</p>
  {:else if item.lane === 'charxp'}
    <p class="note">{COPY.stall.charxp_note}</p>
  {:else if item.lane === 'role'}
    <p class="note">The role appears on your Discord account within a few minutes.</p>
  {/if}
  {#if error}<p class="err" role="alert">{error}</p>{/if}

  {#snippet footer()}
    <button class="ghost" onclick={onClose} disabled={busy}>Cancel</button>
    <button class="go" onclick={buy} disabled={busy || (game && chars.length > 1 && !pick)}>{busy ? 'Buying' : error ? 'Try again' : 'Buy'}</button>
  {/snippet}
</Modal>

<style>
  .cost { margin: 0 0 var(--space-3); display: flex; align-items: baseline; gap: var(--space-2); flex-wrap: wrap; }
  .price { font-size: var(--text-2xl); font-weight: 700; color: var(--ls-amber-bright); }
  .after { color: var(--text-muted); font-size: var(--text-sm); }
  .blurb, .note { color: var(--text-muted); font-size: var(--text-sm); margin: 0 0 var(--space-3); }
  fieldset { border: 0; padding: 0; margin: 0 0 var(--space-3); display: flex; flex-direction: column; gap: var(--space-2); }
  legend { font-family: var(--font-display); text-transform: uppercase; margin-bottom: var(--space-2); padding: 0; }
  label { display: flex; gap: var(--space-2); align-items: center; min-height: 36px; cursor: pointer; }
  input[type='radio'] { width: 18px; height: 18px; accent-color: var(--ls-melange-soft); }
  .err { color: var(--ls-red); font-size: var(--text-sm); margin: 0; }
  button { min-height: 44px; padding: 0 var(--space-4); border-radius: var(--radius-sm); cursor: pointer; font-family: var(--font-display); letter-spacing: .06em; text-transform: uppercase; }
  .ghost { background: transparent; color: var(--text-muted); border: 1px solid var(--edge); }
  .go { background: linear-gradient(180deg, #3a2463, #1b1033); color: #f3ecff; border: 1px solid var(--ls-melange-hi); }
  button:disabled { opacity: .6; cursor: not-allowed; }
</style>
