<script>
  // Tier 5 "Send from bank" cross-player transfer. Entry point only on bank inv30
  // items (ItemCell gates panel==='bank'). The recipient is addressed EITHER by
  // character name OR by identity code and is resolved server-side; the sender is
  // never trusted from the client. A code rides the request BODY and nothing
  // else: this component builds no URL from it.
  //
  // Both gates (LASTSIETCH_ITEM_TRANSFER_ENABLED at the portal, the host flag file at
  // the writer) can be off independently, and either one answers
  // {status:'deferred'}. Deferred means NOTHING MOVED, so it renders as a paused
  // panel and never as a send. The write itself lives in storage.svelte.js so the
  // idempotency key outlives this popover: closing and reopening the dialog must
  // retry the same send, not start a second one.
  import { api } from '$lib/api.js';
  import { storage, sendTransfer } from '$lib/storage.svelte.js';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  let { item, onDone } = $props();

  let mode = $state('name'); // 'name' | 'code'
  let to = $state('');
  let busy = $state(false);
  let phase = $state('idle'); // idle | sent | deferred | failed
  let message = $state('');

  // Send code lane, same shape the Send Solari dialog uses: a lookup has to
  // answer with a player before anything can be sent, and any code refusal
  // retires that confirmation rather than leaving a stale name over a live
  // button. The server re-resolves the code on the write regardless, so a code
  // rotated in between comes back code_unknown.
  const CODE_RE = /^[A-Z2-9]{8}$/;
  let code = $state('');
  let looking = $state(false);
  let note = $state(null);     // { tone, text }
  let resolved = $state(null); // { name, dailyRemaining, pairRemaining }

  let codeValid = $derived(CODE_RE.test(code));

  // OFFLINE GATE, same treatment the move path gets (OnlineGateBanner / ItemCell.canDrag /
  // RepairControls). A transfer TAKES the item out of YOUR bank, and a take from a loaded
  // session is restored under its original id, so the send is locked unless we can confirm
  // you are logged out. Undetermined counts as locked. The server hard-gates at the route
  // and again in the writer; this is honest UI, not the security boundary. The page banner
  // says why, but this dialog is a popover that can sit well below it, so it repeats the
  // reason inline rather than just greying the button out.
  let locked = $derived(!storage.offlineOk);
  let undetermined = $derived(storage.online == null);

  // Either flag being off, or a deferred answer from the write, is the same fact
  // for the player: transfer is paused and the item did not move.
  let paused = $derived(!storage.transferEnabled || phase === 'deferred');

  let name = $derived(item?.name || item?.template || 'item');
  let dailyLeft = $derived(Math.max(0, storage.transferDailyCap - storage.transferDailyUsed));
  // The per-player count is only KNOWN once a lookup has answered. On the name
  // lane there is no lookup, so the dialog states the cap as a cap rather than
  // printing it as a remaining count the server never gave us.
  let pairLeft = $derived(resolved?.pairRemaining ?? storage.transferPairCap);
  let hasRecipient = $derived(mode === 'code' ? !!resolved : to.trim().length > 0);
  let canSend = $derived(!busy && !locked && hasRecipient);

  function pickMode(m) {
    mode = m;
    phase = 'idle';
    message = '';
  }

  function onCode(e) {
    code = e.target.value.toUpperCase().replace(/[^A-Z2-9]/g, '').slice(0, 8);
    e.target.value = code;
    resolved = null;
    note = null;
  }

  async function onLookup(e) {
    e.preventDefault();
    if (looking) return;
    resolved = null;
    if (!codeValid) {
      note = { tone: 'warn', text: 'A code is eight characters, A to Z and 2 to 9.' };
      return;
    }
    looking = true; note = null;
    try {
      const r = await api.settings.codes.lookup(code);
      if (r?.found) {
        resolved = {
          name: r.display_name,
          // null stays null: an unknown allowance is never printed as zero.
          dailyRemaining: r.daily_remaining ?? null,
          pairRemaining: r.pair_remaining ?? null,
        };
      } else {
        note = { tone: 'warn', text: 'No such code. Check the eight characters and try again.' };
      }
    } catch (err) {
      note = err?.status === 429
        ? { tone: 'error', text: err?.data?.message || 'Too many lookups just now. Wait a moment and try again.' }
        : { tone: 'error', text: err?.data?.message || 'That lookup could not be answered right now.' };
    } finally {
      looking = false;
    }
  }

  async function send() {
    if (!canSend) return;
    busy = true; phase = 'idle'; message = '';
    const who = mode === 'code' ? resolved.name : to.trim();
    const r = await sendTransfer({
      itemId: item?.item_id,
      template: item?.template || '',
      code: mode === 'code' ? code : '',
      charName: mode === 'code' ? '' : to.trim(),
    });
    phase = r.phase;
    if (r.phase === 'sent') {
      message = r.message || `Sent ${name} to ${who}.`;
      setTimeout(() => onDone?.(), 1100);
    } else if (r.phase === 'deferred') {
      // The paused panel carries the copy; a status line under it would be a
      // second, quieter claim about the same non-event.
      message = '';
      resolved = null;
    } else {
      message = r.message;
      // A code refusal invalidates the confirmed recipient, so it goes with it.
      if (mode === 'code') resolved = null;
    }
    busy = false;
  }
</script>

<div class="form">
  <p class="head mono">Send from bank &middot; {name}</p>

  {#if paused}
    <SealedPanel
      status="empty" action="none" art="empty-vault" width="prose"
      emptyText="Item transfer is paused right now. Nothing was moved, and nothing leaves your bank until it is switched back on."
    />
    <div class="row">
      <button class="btn" type="button" onclick={() => onDone?.()}>Close</button>
    </div>
  {:else}
    {#if locked}
      <p class="gate" role="note">
        {#if undetermined}
          Cannot confirm you are logged out, so sending is locked. Items only leave your bank while you are offline.
        {:else}
          Log out of the game to send an item. Items only leave your bank while you are offline.
        {/if}
      </p>
    {/if}

    <div class="tabs" role="tablist" aria-label="Recipient">
      <button
        role="tab" aria-selected={mode === 'name'} class:on={mode === 'name'}
        type="button" onclick={() => pickMode('name')}
      >Character name</button>
      <button
        role="tab" aria-selected={mode === 'code'} class:on={mode === 'code'}
        type="button" onclick={() => pickMode('code')}
      >Send code</button>
    </div>

    {#if mode === 'name'}
      <label class="fld"><span class="lbl">To character</span>
        <input type="text" bind:value={to} placeholder="Character name" aria-label="Recipient character name" disabled={locked} />
      </label>
    {:else}
      <form class="lookup" onsubmit={onLookup}>
        <label class="fld"><span class="lbl">Send code</span>
          <input
            class="mono"
            type="text"
            autocomplete="off"
            autocapitalize="characters"
            spellcheck="false"
            maxlength="8"
            placeholder="ABCD2345"
            value={code}
            oninput={onCode}
            aria-label="Recipient identity code"
          />
        </label>
        <button class="btn" type="submit" disabled={looking || !codeValid}>{looking ? 'Looking' : 'Look up'}</button>
      </form>
      {#if note}<Notice tone={note.tone} text={note.text} />{/if}
      {#if resolved}
        <p class="confirm">Sending to {resolved.name}</p>
      {/if}
    {/if}

    <!-- What the player is agreeing to, before the button and not after it. -->
    <ul class="terms">
      <li>The whole stack moves. A send cannot be split.</li>
      <li>The item leaves your bank only while you are logged out.</li>
      <li>
        {dailyLeft} of your sends left today{#if resolved && resolved.pairRemaining != null}, {pairLeft} left to this player{:else}, and at most {pairLeft} to any one player{/if}.
      </li>
      <li>The recipient sees it in their CHOAM bank at their next zone transition.</li>
    </ul>

    <div class="row">
      <button class="btn primary" type="button" onclick={send} disabled={!canSend}>{busy ? 'Sending' : 'Send'}</button>
      <button class="btn" type="button" onclick={() => onDone?.()}>Cancel</button>
    </div>
    {#if phase !== 'idle' && message}
      <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
    {/if}
  {/if}
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--space-2); }
  .head { margin: 0; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .1em; }
  .gate {
    margin: 0; font-size: var(--text-xs); line-height: 1.4; color: var(--text-muted);
    padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--ls-yellow) 45%, var(--edge));
    background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0));
  }
  .tabs { display: inline-flex; gap: var(--space-1); border-bottom: 1px solid var(--edge); }
  .tabs button {
    flex: 0 0 auto; background: transparent; color: var(--text-muted); border: 0;
    border-bottom: 2px solid transparent; cursor: pointer; white-space: nowrap;
    padding: var(--space-1) var(--space-2); font-size: var(--text-xs);
    font-family: var(--font-mono); letter-spacing: .1em; text-transform: uppercase;
  }
  .tabs button.on { color: var(--accent-bright); border-bottom-color: var(--accent); font-weight: 700; }
  /* min-width:0 lets these shrink inside the 1fr 1fr grid when the popover is
     width-capped to a narrow panel; grid/flex items default to min-width:auto
     and would otherwise refuse to go below their content width. */
  .fld { min-width: 0; flex: 1 1 8rem; display: flex; flex-direction: column; gap: var(--space-1); }
  .lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .fld input {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); text-transform: none; letter-spacing: normal;
  }
  .fld input.mono { font-family: var(--font-mono); letter-spacing: .18em; }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .lookup { display: flex; align-items: flex-end; gap: var(--space-2); }
  .confirm { margin: 0; font-size: var(--text-sm); color: var(--text); }
  .terms {
    margin: 0; padding-left: var(--space-4); display: flex; flex-direction: column; gap: 2px;
    font-size: var(--text-xs); line-height: 1.4; color: var(--text-muted);
  }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); }
  .status[data-phase='sent'] { color: var(--ls-green); }
  .status[data-phase='deferred'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
