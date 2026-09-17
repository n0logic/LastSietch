<script>
  // Solari gift. The sender is server-derived from the session; the recipient is
  // addressed EITHER by character name OR by identity code (never both), and both
  // are resolved server-side. Gifting ships DARK behind LASTSIETCH_GIFTS_ENABLED: a
  // status:'deferred' response is surfaced honestly as "not yet enabled", so the
  // UI never implies Solari moved when the backend is dark.
  //
  // A code rides the request BODY and nothing else. It is never put in a URL,
  // never echoed into a link, and the server re-resolves it on the write, so a
  // code rotated between the confirm step and Send comes back `code_unknown`
  // rather than falling through to the name the sender saw.
  import { api, uuidv4 } from '$lib/api.js';

  // maxAmount mirrors GIFT_MAX_AMOUNT (the writer is the real gate; this is a
  // clean client-side message instead of waiting on a server refusal).
  // recipientCode and recipientCharName are mutually exclusive: a code wins when
  // both are passed, and the name field is not rendered at all in code mode.
  //
  // The two DAILY caps have no player-facing source to read: portal.py serves
  // neither, and routers/v2_systems.py:1349-1351 publishes all three only on the
  // owner systems board, which a player cannot reach. So they are mirrored here
  // as literals from portal_gift_limits.GIFT_MAX_PER_PAIR_PER_DAY (5) and
  // GIFT_MAX_PER_DAY (20). Both are env-overridable on the server, so if either
  // is ever retuned this pair of numbers has to move with it. The per-gift
  // ceiling is NOT duplicated: it comes from the maxAmount prop above, which is
  // the same value the over-cap warning below already prints.
  const GIFTS_PER_PAIR_PER_DAY = 5;
  const GIFTS_PER_DAY = 20;

  let {
    recipientCharName = '',
    recipientCode = '',
    locked = false,
    onSent,
    onRefused,
    maxAmount = 5_000_000,
  } = $props();

  // svelte-ignore state_referenced_locally
  let to = $state(recipientCharName || '');
  let amount = $state('');
  let busy = $state(false);
  // phase: 'idle' | 'sent' | 'deferred' | 'failed'
  let phase = $state('idle');
  let message = $state('');

  // Wave 7 code refusals. Every other token keeps the backend's own copy.
  const CODE_REFUSAL_TEXT = {
    bad_code: 'A code is eight characters, A to Z and 2 to 9. Nothing was sent.',
    code_unknown: 'No player carries that code now. It may have been rotated. Nothing was sent.',
    lookup_throttled: 'Too many code lookups just now. Wait a moment, then try again.',
    linked_alt: 'That code belongs to an account linked to yours. Nothing was sent.',
    self_transfer: 'That is your own code. Nothing was sent.',
    recipient_unlinked: 'No player by that name has opened a bank. Nothing was sent.',
  };

  // One idempotency key per LOGICAL send, reused across retries. Kept only
  // while the outcome is unknown (a throw: timeout, dropped connection) so the
  // retry replays instead of double-sending; any definitive server answer --
  // applied, replay, deferred, or a refusal -- ends the logical send and the
  // next submit is a new one. Re-minted if the recipient or amount changes.
  let idemKey = null;
  let idemFor = '';

  let byCode = $derived(recipientCode.length > 0);
  let amountNum = $derived(Math.floor(Number(amount)) || 0);
  let overCap = $derived(amountNum > maxAmount);
  let hasRecipient = $derived(byCode || to.trim().length > 0);
  let canSend = $derived(!busy && hasRecipient && amountNum > 0 && !overCap);

  async function send() {
    if (!canSend) return;
    busy = true; phase = 'idle'; message = '';
    const sig = `${byCode ? recipientCode : to.trim()}|${amountNum}`;
    if (!idemKey || idemFor !== sig) { idemKey = uuidv4(); idemFor = sig; }
    try {
      const body = { amount: amountNum, idempotency_key: idemKey };
      if (byCode) body.recipient_code = recipientCode;
      else body.recipient_char_name = to.trim();
      const r = await api.messages.giftSend(body);
      idemKey = null; // definitive answer either way -- next submit is a new send
      const status = r?.status || (r?.success ? 'applied' : 'failed');
      if (status === 'applied' || status === 'replay') {
        const who = byCode ? 'that player' : to.trim();
        phase = 'sent'; message = r?.message || `Sent ${amountNum} Solari to ${who}.`;
        amount = '';
        onSent?.(r);
      } else if (status === 'deferred') {
        phase = 'deferred'; message = r?.message || 'Gifting is not yet enabled. Nothing was sent.';
      } else {
        phase = 'failed'; message = r?.message || 'The gift did not send.';
      }
    } catch (e) {
      // e.message is a REFUSAL TOKEN or, with no envelope at all, the raw
      // "POST path -> 404" line sendCsrfJSON builds: neither is player copy, so
      // it is read as a token and never displayed. The friendly sentence comes
      // from this map or from e.data.message (the backend's _fail() envelope),
      // and the last resort is our own literal.
      // A refusal IS an answer: end the logical send so the next submit is a new
      // one. A timeout is not, so the key survives and the retry replays.
      if (typeof e?.status === 'number') idemKey = null;
      // The signature goes with the key: leaving it behind makes the next
      // submit look like a retry of a send that is already over.
      if (!idemKey) idemFor = '';
      const token = e?.message || '';
      phase = 'failed';
      message = CODE_REFUSAL_TEXT[token] || e?.data?.message
        || 'The exchange could not be reached.';
      // A code refusal invalidates the confirmed recipient the caller is showing,
      // so it has to hear about it rather than leaving a stale name on screen.
      if (CODE_REFUSAL_TEXT[token]) onRefused?.({ error: token, message });
    } finally { busy = false; }
  }
</script>

<div class="gift">
  <p class="kicker mono">Send Solari</p>
  <!-- Mechanics note. Players confuse this with Withdraw on the Storage page,
       which is a different action on the same currency, so the panel says what
       this one does before it asks who to send to.
       Only on the STANDALONE mount (/guilds). The three inline mounts inside
       SendSolariDialog pass locked={true} and already have a resolved
       recipient, so there the note would repeat once per expanded row, and its
       closing line would be pointing a player who is already on the Storage
       page at the Storage page. -->
  {#if !locked}
    <div class="how">
      <p class="kicker sub mono">How it works</p>
      <p>This moves banked Solari from your CHOAM account balance to the recipient's. It leaves the moment you press Gift and is credited straight away: no logging out, and no items involved.</p>
      <p>The recipient is looked up by character name, and they must have linked their character on the portal. You cannot gift yourself, or any account linked to your own.</p>
      <p>Caps are <span class="fig">{maxAmount.toLocaleString()}</span> Solari per gift, <span class="fig">{GIFTS_PER_PAIR_PER_DAY}</span> gifts to the same player per day, and <span class="fig">{GIFTS_PER_DAY}</span> gifts a day in total.</p>
      <p>Looking to move your own Solari into your backpack instead? That is Withdraw on the Storage page, and it needs you logged out.</p>
    </div>
  {/if}
  <div class="row">
    {#if !byCode}
      <label class="fld"><span>To</span>
        <input type="text" bind:value={to} readonly={locked} placeholder="Character name" aria-label="Recipient character name" />
      </label>
    {/if}
    <label class="fld amt"><span>Amount</span>
      <input
        type="number" min="1" step="1" bind:value={amount} placeholder="0" aria-label="Solari amount"
        class:err={overCap}
      />
    </label>
    <button class="btn primary" type="button" onclick={send} disabled={!canSend}>{busy ? 'Sending' : 'Gift'}</button>
  </div>
  {#if overCap}
    <p class="warn mono" role="alert">Over the {maxAmount.toLocaleString()} Solari cap for one gift.</p>
  {/if}
  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</div>

<style>
  .gift { display: flex; flex-direction: column; gap: var(--space-2); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  /* Sub-caption under the panel title: same amber, tighter tracking so it reads
     as a caption rather than as a second panel heading. */
  .kicker.sub { letter-spacing: .12em; }
  .how { display: flex; flex-direction: column; gap: var(--space-2); max-width: 64ch; }
  .how p { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.55; }
  .how p.kicker { color: var(--accent); }
  /* Cap figures are settings, not live values, so amber and never Ibad. */
  .fig { color: var(--accent-text); font-variant-numeric: tabular-nums; }

  .row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--space-2); }
  .fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; text-transform: uppercase; flex: 1 1 10rem; }
  .fld.amt { flex: 0 1 7rem; }
  .fld input {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
    text-transform: none; letter-spacing: normal;
  }
  .fld input[readonly] { color: var(--text-muted); background: var(--bg-elevated); }
  .fld input.err { border-color: var(--ls-red); }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .warn { margin: 0; font-size: var(--text-xs); color: var(--ls-red); }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-sm); cursor: pointer;
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { font-size: var(--text-sm); margin: 0; }
  .status[data-phase='sent'] { color: var(--ls-green); }
  .status[data-phase='deferred'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
